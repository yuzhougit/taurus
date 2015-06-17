"""
Module holds selenium stuff
"""

import os

from bzt.engine import ScenarioExecutor, Scenario
# from bzt.modules.console import WidgetProvider
from subprocess import CalledProcessError
import subprocess
import traceback
from six.moves.urllib.request import URLopener
from bzt.utils import download_progress_hook, unzip, shell_exec, humanize_time
import time
import signal
import tempfile
import logging
import shutil
import sys



# try:
#     from lxml import etree
# except ImportError:
#     try:
#         import cElementTree as etree
#     except ImportError:
#         import elementtree.ElementTree as etree


class SeleniumExecutor(ScenarioExecutor):
    """
    Selenium executor
    """
    SELENIUM_DOWNLOAD_LINK = "http://selenium-release.storage.googleapis.com/{version}/" \
                             "selenium-server-standalone-{version}.0.jar"
    SELENIUM_VERSION = "2.46"
    RUNNER = None

    def __init__(self):
        super(SeleniumExecutor, self).__init__()
        self.selenium_log = None
        self.widget = None
        self.reader = None
        self.is_grid = None
        self.scenario = None
        self.start_time = None
        self.end_time = None
        self.hub_process = None
        self.node_process = None
        self.stdout_hub = None
        self.stderr_node = None
        self.stdout_node = None
        self.stderr_hub = None

    def prepare(self):
        """
        1) Locate script or folder
        2) check type py/java
        2.5) check if we need grid server
        3) check tool readiness (maven or nose + pip selenium)
        :return:
        """
        self.selenium_log = self.engine.create_artifact("selenium", ".log")
        self.scenario = self.get_scenario()
        self.is_grid = self.scenario.get("use-grid", False)
        self.__run_checklist()

        script_type, script_is_folder = self.detect_script_type(self.scenario.get("script"))

        if script_type == "java":
            maven_config = self.settings.get("selenium-tools").get("maven")
            SeleniumExecutor.RUNNER = Maven(maven_config)
        elif script_type == "python":
            nose_config = self.settings.get("selenium-tools").get("nose")
            SeleniumExecutor.RUNNER = NoseTester(nose_config)
        elif script_type == "jar":
            junit_config = self.settings.get("selenium-tools").get("junit")
            SeleniumExecutor.RUNNER = JunitTester(junit_config)

        if not SeleniumExecutor.RUNNER.check_if_installed():
            SeleniumExecutor.RUNNER.install()

        tests_scripts_folder = os.path.join(self.engine.artifacts_dir, "selenium_scripts")  # should be set in config

        if Scenario.SCRIPT in self.scenario:
            if script_is_folder:
                shutil.copytree(self.scenario.get("script"), tests_scripts_folder)
            else:
                os.makedirs(tests_scripts_folder)
                shutil.copy2(self.scenario.get("script"), tests_scripts_folder)

    def detect_script_type(self, script_path):
        """
        checks if script is java or python
        if it's folder or single script
        :return:
        """
        if os.path.isdir(script_path):
            self.log.info("processing all scripts in a folder %s", script_path)
            for script_file in os.listdir(script_path):
                file_ext = os.path.splitext(script_file)[1].lower()
                if file_ext == ".java":
                    self.log.info("detected script type: java")
                    return "java", True
                elif file_ext == ".py":
                    self.log.info("detected script type: python")
                    return "python", True
                elif file_ext == ".jar":
                    self.log.info("detected script type: jar")
                    return "jar", True
            self.log.error("Unknown script type.")
            raise BaseException("unknown script type")
        else:
            self.log.info("checking type of script %s", script_path)
            file_ext = os.path.splitext(script_path)[1]
            if file_ext == ".java":
                self.log.info("detected script type: java")
                return "java", False
            elif file_ext == ".py":
                self.log.info("detected script type: python")
                return "python", False
            elif file_ext == ".jar":
                self.log.info("detected script type: jar")
                return "jar", False
            else:
                self.log.error("Unknown script type.")
                raise BaseException("unknown script type")

    def startup(self):
        """
        Start selenium server, execute script
        :return:
        """
        # TODO: implement selenium-server grid
        self.start_time = time.time()
        if self.is_grid:
            selenium_hub_cmdline = ["java", "-jar", os.path.realpath(self.settings.get("path")), "-role",
                                    "hub"]  # , "-debug", "-log", "test.log"
            selenium_node_cmdline = ["java", "-jar", os.path.realpath(self.settings.get("path")), "-role", "webdriver",
                                     "-port 5555", "-hub http://127.0.0.1:4444/grid/register",
                                     "-browser browserName=firefox"]
            hub_out = self.engine.create_artifact("selenium-hub-stdout", ".log")
            hub_err = self.engine.create_artifact("selenium-hub-stderr", ".log")
            self.stdout_hub = open(hub_out, "w")
            self.stderr_hub = open(hub_err, "w")

            node_out = self.engine.create_artifact("selenium-node-stdout", ".log")
            node_err = self.engine.create_artifact("selenium-node-stderr", ".log")
            self.stdout_node = open(node_out, "w")
            self.stderr_node = open(node_err, "w")

            self.hub_process = shell_exec(selenium_hub_cmdline, cwd=self.engine.artifacts_dir,
                                          stdout=self.stdout_hub,
                                          stderr=self.stderr_hub)

            self.node_process = shell_exec(selenium_node_cmdline, cwd=self.engine.artifacts_dir,
                                           stdout=self.stdout_node,
                                           stderr=self.stderr_node)

        SeleniumExecutor.RUNNER.run_tests(self.engine.artifacts_dir, self.get_scenario())

    def check(self):
        """
        check if test completed
        :return:
        """
        # if self.widget:
        #    self.widget.update()

        return SeleniumExecutor.RUNNER.is_finished()

    def shutdown(self):
        """
        shutdown test_runner, shutdown selenium-server hub/node if grid
        :return:
        """
        if self.is_grid:
            while self.hub_process and self.hub_process.poll() is None:
                self.log.info("Terminating selenium-server PID: %s", self.hub_process.pid)
                time.sleep(1)
                try:
                    os.killpg(self.hub_process.pid, signal.SIGTERM)
                except OSError as exc:
                    self.log.debug("Failed to terminate: %s", exc)

                if self.stdout_hub:
                    self.stdout_hub.close()
                if self.stderr_hub:
                    self.stderr_hub.close()

        SeleniumExecutor.RUNNER.prepare_logs(self.engine.artifacts_dir)

        if self.start_time:
            self.end_time = time.time()
            self.log.info("Selenium tests run for %s seconds",
                          self.end_time - self.start_time)

    def __run_checklist(self):
        """
        Check all tools: java, maven, selenium-server, installed selenium package (python)
        :return:
        """
        selenium_path = self.settings.get("path", "~/selenium-taurus/selenium-server.jar")
        selenium_path = os.path.abspath(os.path.expanduser(selenium_path))
        selenium_tools_path = self.settings.get("tools-path", "~/selenium-taurus/tools/")
        selenium_tools_path = os.path.abspath(os.path.expanduser(selenium_tools_path))
        self.settings['path'] = selenium_path
        self.settings['tools-path'] = selenium_tools_path

        try:
            self.__check_java()
        except (OSError, CalledProcessError):
            self.log.debug("Failed to run java: %s", traceback.format_exc())
            return

        try:
            self.__check_selenium_server(selenium_path)
        except (OSError, CalledProcessError):
            self.log.debug("Failed to run selenium-server: %s", traceback.format_exc())
            # try install selenium-server
            self.__install_selenium_server(selenium_path)
            self.__check_selenium_server(selenium_path)
            return

    def __check_selenium_server(self, selenium_path):
        """
        Check if selenium server working

        :return: Bool
        """
        self.log.debug("Trying selenium-server: %s > %s", selenium_path, self.selenium_log)
        selenium_launch_command = ["java", "-jar", selenium_path, "-help"]
        selenium_subproc = subprocess.Popen(selenium_launch_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selenium_output = selenium_subproc.communicate()[0]

        if selenium_subproc.returncode != 0:
            raise CalledProcessError(selenium_subproc.returncode, " ".join(selenium_launch_command))
        self.log.debug("Selenium check: %s", selenium_output)

    def __install_selenium_server(self, dest_path):
        """
        Download and install selenium-server
        :param dest_path: path
        :return:
        """

        dest = os.path.dirname(os.path.expanduser(dest_path))
        if not dest:
            dest = "selenium-taurus"
        dest = os.path.abspath(dest)
        if not os.path.exists(dest):
            os.makedirs(dest)

        selenium_path = os.path.join(dest, "selenium-server.jar")
        try:
            self.__check_selenium_server(selenium_path)
            return selenium_path
        except (OSError, CalledProcessError):
            self.log.info("Will try to install selenium server into %s", dest)

        downloader = URLopener()
        selenium_server_download_link = self.settings.get("download-link", SeleniumExecutor.SELENIUM_DOWNLOAD_LINK)
        selenium_version = self.settings.get("version", SeleniumExecutor.SELENIUM_VERSION)
        selenium_server_download_link = selenium_server_download_link.format(version=selenium_version)
        self.log.info("Downloading %s", selenium_server_download_link)
        try:
            downloader.retrieve(selenium_server_download_link, selenium_path, download_progress_hook)
        except BaseException as exc:
            self.log.error("Error while downloading %s", selenium_server_download_link)
            raise exc

    def __check_java(self):
        """
        Check java
        :return:
        """
        try:
            jout = subprocess.check_output(["java", '-version'], stderr=subprocess.STDOUT)
            self.log.debug("Java check: %s", jout)
        except BaseException:
            self.log.warning("Failed to run java: %s", traceback.format_exc())
            raise RuntimeError("The 'java' is not operable or not available. Consider installing it")


class AbstractTestRunner(object):
    """
    Abstract class test runner
    """

    def __init__(self, settings):
        self.process = None
        self.settings = settings
        self.log = logging.getLogger('')
        self.report_files = []

    def check_if_installed(self):
        raise NotImplementedError

    def install(self):
        raise NotImplementedError

    def run_tests(self, artifacts_dir, scenario):
        raise NotImplementedError

    def is_finished(self):
        raise NotImplementedError

    def prepare_logs(self, artifacts_dir):
        raise NotImplementedError


class Maven(AbstractTestRunner):
    MAVEN_DOWNLOAD_LINK = "http://apache-mirror.rbc.ru/pub/apache/maven/maven-3/{version}/" \
                          "binaries/apache-maven-{version}-bin.zip"
    MAVEN_VERSION = "3.3.3"

    def __init__(self, maven_config):
        super(Maven, self).__init__(maven_config)
        self.maven_path = self.settings.get("path", "~/selenium-taurus/tools/maven/bin/mvn")
        self.maven_path = os.path.abspath(os.path.expanduser(self.maven_path))
        self.pom_file = PomFile(self.settings.get("pom_file", ""))

    def check_if_installed(self):
        """
        Check if maven installed
        """
        try:
            subprocess.check_output(["mvn", "-v"], stderr=subprocess.STDOUT)  # check if installed globally
            return True
        except (OSError, CalledProcessError):
            try:
                subprocess.check_output([self.maven_path, "-v"], stderr=subprocess.STDOUT)
                self.maven_path = os.path.abspath(self.maven_path)
                return True
            except (OSError, CalledProcessError):
                return False

    def install(self):
        """
        Install maven
        """
        dest = os.path.dirname(os.path.dirname(os.path.expanduser(self.maven_path)))
        if not dest:
            dest = "selenium-taurus/tools/maven/"
        dest = os.path.abspath(dest)

        if not os.path.exists(dest):
            os.makedirs(dest)

        with tempfile.NamedTemporaryFile() as maven_zip_path:
            downloader = URLopener()
            maven_download_link = self.settings.get("download-link", Maven.MAVEN_DOWNLOAD_LINK)
            maven_version = self.settings.get("maven-version", Maven.MAVEN_VERSION)
            maven_download_link = maven_download_link.format(version=maven_version)
            self.log.info("Downloading %s", maven_download_link)
            try:
                downloader.retrieve(maven_download_link, maven_zip_path.name, download_progress_hook)
            except BaseException as exc:
                self.log.error("Error while downloading %s", maven_download_link)
                raise exc

            self.log.info("Unzipping %s", os.path.join(dest, "maven"))
            unzip(maven_zip_path.name, dest, "apache-maven-%s" % Maven.MAVEN_VERSION)
            os.chmod(self.maven_path, 0o755)
            self.log.info("Installed maven successfully")
            return self.maven_path

    def run_tests(self, artifacts_dir, scenario):
        """
        run tests
        1) generate pom
        2) save pom into artifacts
        3) execute maven tests
        """
        self.pom_file.generate_pom()
        self.pom_file.save(os.path.join(artifacts_dir, "pom.xml"))

        maven_out = open(os.path.join(artifacts_dir, "mvn_out"), 'wb')
        maven_err = open(os.path.join(artifacts_dir, "mvn_err"), 'wb')
        self.process = shell_exec(self.maven_path + " test -fn", cwd=artifacts_dir, stdout=maven_out, stderr=maven_err)

    def prepare_logs(self, artifacts_dir):
        """
        get all report files
        :param artifacts_dir: path
        :return:
        """
        report_folder = os.path.join(artifacts_dir, "selenium_scripts", "reports", "surefire-reports")
        report_folder = os.path.abspath(os.path.expanduser(report_folder))
        report_files = [file_path for file_path in os.listdir(report_folder) if file_path.endswith(".txt")]
        for report_file in report_files:
            self.report_files.append(os.path.join(report_folder, report_file))

    def is_finished(self):
        ret_code = self.process.poll()
        if ret_code is not None:
            if ret_code != 0:
                self.log.info("test runner exit code: %s", ret_code)
                raise RuntimeError("test runner exited with non-zero code")
            return True
        return False


class PomFile(object):
    def __init__(self, existing_pom_file_path):
        self.pom_file_path = existing_pom_file_path
        self.xml_tree = b""

    def generate_pom(self):
        """
        Generate POM from scratch or use and modify existing pom
        :return:
        """
        #  TODO implement xml modification
        base_pom = b"""<?xml version="1.0" encoding="UTF-8"?>
<project>
<modelVersion>4.0.0</modelVersion>
<groupId>com.example.tests</groupId>
<artifactId>test_jar</artifactId>
<version>1.0.0-SNAPSHOT</version>
<build>
<directory>selenium_scripts/reports</directory>
<outputDirectory>selenium_scripts/compiled/classes</outputDirectory>
<testOutputDirectory>selenium_scripts/compiled/test-classes</testOutputDirectory>
<sourceDirectory>selenium_scripts</sourceDirectory>
<scriptSourceDirectory>selenium_scripts</scriptSourceDirectory>
<testSourceDirectory>selenium_scripts</testSourceDirectory>
</build>
<dependencies>
<dependency>
<groupId>junit</groupId>
<artifactId>junit</artifactId>
<version>4.11</version>
</dependency>
<dependency>
<groupId>org.seleniumhq.selenium</groupId>
<artifactId>selenium-java</artifactId>
<version>LATEST</version>
</dependency>
</dependencies>
<reporting>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-report-plugin</artifactId>
        <version>2.18.1</version>
        <configuration>
          <outputDirectory>surefire</outputDirectory>
        </configuration>
      </plugin>
    </plugins>
  </reporting>
</project>"""

        self.xml_tree = base_pom

        # if not self.pom_file_path:
        #    root = etree.Element(b"project")
        #    self.xml_tree = etree.Element(root)
        #    self.xml_tree.append(etree.fromstring(base_pom))
        # else:
        #    with open(self.pom_file_path, 'rb') as fds:
        #        self.xml_tree = etree.parse(fds)

        # self.modify_test_dir()

    def save(self, path):
        """
        save pom to file
        :param path:
        :return:
        """
        with open(path, "wb") as fds:
            fds.write(self.xml_tree)
            # fds.write(self.xml_tree.tostring, pretty_print=True, encoding="UTF-8", xml_declaration=True)

            # def modify_test_dir(self):
            #     project_tag = self.xml_tree.find("project")
            #     build_tag = etree.Element("build")
            #     test_path = etree.Element("testSourceDirectory")
            #     test_path.text = "selenium_scripts"
            #     build_tag.append(test_path)
            #     project_tag.append(build_tag)


class JunitTester(AbstractTestRunner):
    """
    Allows to test jar files
    """
    # TODO: implement support of .class files, custom junit result listener
    def __init__(self, junit_config):
        super(JunitTester, self).__init__(junit_config)
        self.junit_path = self.settings.get("path", "~/selenium-taurus/tools/junit/junit.jar")
        self.junit_path = os.path.abspath(os.path.expanduser(self.junit_path))

    def check_if_installed(self):
        return True

    def install(self):
        # TODO: implement custom junit installation
        return self.junit_path

    def run_tests(self, artifacts_dir, scenario):
        # java -cp junit.jar:selenium-test-small.jar:
        # selenium-2.46.0/selenium-java-2.46.0.jar:./../selenium-server.jar
        # org.junit.runner.JUnitCore TestBlazemeterPass

        junit_class_path = self.junit_path
        test_jar_path = os.path.abspath(scenario.get("script"))  # TODO: should list all jars and add them in cp
        selenium_java = os.path.expanduser(self.settings.get("selenium-libs"))
        selenium_server = os.path.expanduser("~/selenium-taurus/selenium-server.jar")
        taurus_test_suite = os.path.abspath(os.path.expanduser("tests/selenium/junit_listener/taurus_testsuite.jar"))

        if not os.path.isdir(test_jar_path):

            junit_command_line = ["java", "-cp",
                                  ":".join([junit_class_path, test_jar_path, selenium_java, selenium_server, taurus_test_suite]),
                                  "taurus_junit_testsuite.CustomTestSuite", test_jar_path]

            # self.log.info(junit_command_line)
            junit_out_path = os.path.join(artifacts_dir, "junit_out")
            junit_err_path = os.path.join(artifacts_dir, "junit_err")

            junit_out = open(junit_out_path, 'wb')
            junit_err = open(junit_err_path, 'wb')

            self.process = shell_exec(junit_command_line, cwd=artifacts_dir,
                                      stdout=junit_out,
                                      stderr=junit_err)

            self.report_files.append(os.path.join(artifacts_dir, "report.txt"))

        else:
            raise RuntimeError("Testing with more then one jar file currently not implemented")

    def prepare_logs(self, artifacts_dir):
        pass

    def is_finished(self):
        ret_code = self.process.poll()
        if ret_code is not None:
            if ret_code != 0:
                self.log.info("test runner exit code: %s", ret_code)
                raise RuntimeError("test runner exited with non-zero code")
            return True
        return False


class NoseTester(AbstractTestRunner):
    """
    Python selenium tests runner
    """
    # TODO: implement custom nose report plugin
    def __init__(self, nose_config):
        super(NoseTester, self).__init__(nose_config)

    def check_if_installed(self):
        """
        nose and selenium packages are required.
        """
        try:
            import selenium

            self.log.info("selenium already installed")
            return True
        except ImportError:
            self.log.info("missing selenium packages")
            return False

    def install(self):
        """
        install nose, selenium from pip
        """
        # FIXME: We should not install those if python3 (we need python2 to run selenium tests)

        try:
            import pip
            pip.main(['install', "nose"])
            pip.main(['install', "selenium"])
            self.log.info("selenium packages were successfully installed")
        except BaseException as exc:
            self.log.debug("Error while installing additional package selenium %s", traceback.format_exc())
            raise RuntimeError("Error while installing selenium %s" % exc)

    def run_tests(self, artifacts_dir, scenario):
        """
        run python tests
        """
        env = os.environ.copy()
        executable = sys.executable
        nose_command_line = [executable, "-m", "nose", "--with-taurus_nose_plugin", "selenium_scripts"]
        self.log.info(nose_command_line)
        nose_out = open(os.path.join(artifacts_dir, "nose_out"), 'ab')
        nose_err = open(os.path.join(artifacts_dir, "nose_err"), 'ab')
        self.process = subprocess.Popen(nose_command_line, cwd=artifacts_dir,
                                        stdout=nose_out,
                                        stderr=nose_err, env=env)
        self.report_files.append(os.path.join(artifacts_dir, "report.txt"))

    def prepare_logs(self, artifacts_dir):
        pass

    def is_finished(self):
        ret_code = self.process.poll()
        if ret_code is not None:
            if ret_code == 0:
                self.log.info("python nose tests exit code: %s", ret_code)
            elif ret_code == 1:
                self.log.info("python nose tests exit code: %s, some tests failed", ret_code)
            return True
        return False

