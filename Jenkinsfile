// Jenkinsfile - CI pipeline for the motor controller SIL rig.
// Needs the "Docker Pipeline" plugin. The agent image (ci/Dockerfile) adds cppcheck
// to python:3.12, which already ships gcc, make and git.

pipeline {
    agent { dockerfile { filename 'ci/Dockerfile' } }

    environment {
        MPLBACKEND = 'Agg'          // plots render headless on the build agent
        PY         = '.venv/bin/python'
    }

    options {
        timeout(time: 20, unit: 'MINUTES')
    }

    triggers {
        pollSCM('H/5 * * * *')      // check GitHub for new commits every ~5 minutes
    }

    stages {
        stage('Setup') {
            steps {
                sh 'python -m venv .venv && $PY -m pip install --quiet -r requirements.txt'
                sh 'rm -rf reports dist && mkdir -p reports'
            }
        }

        stage('Build controller') {
            // Strict compiler flags (-Werror) make this a code-quality gate too
            steps {
                sh 'make -C controller clean && make -C controller'
            }
        }

        stage('Static analysis') {
            steps {
                // Gate: any finding fails the build and is printed in the console log.
                // --check-level=exhaustive analyses every branch (the default limits it
                // and reports that limit as a finding, which would fail the gate).
                sh '''cppcheck --enable=warning,style,performance,portability --std=c99 \
                      --check-level=exhaustive -Icontroller/include \
                      --suppress=missingIncludeSystem --error-exitcode=1 --template=gcc \
                      controller/src controller/platform'''
                // Same analysis as XML, archived as evidence
                sh '''cppcheck --enable=warning,style,performance,portability --std=c99 \
                      --check-level=exhaustive -Icontroller/include \
                      --suppress=missingIncludeSystem --xml --output-file=reports/cppcheck.xml \
                      controller/src controller/platform'''
                // MISRA C:2012 report only - informs review, does not fail the build yet
                sh '''cppcheck --addon=misra --std=c99 -Icontroller/include \
                      --suppress=missingIncludeSystem \
                      controller/src controller/platform 2> reports/misra.txt || true'''
            }
        }

        stage('Calibration check') {
            // Every value inside limits.json, every cross-parameter rule satisfied
            steps {
                sh '$PY -m tools.check_calibration calibration/default.json'
            }
        }

        stage('SIL tests') {
            // Plant physics, CAN interface, and every requirement-linked SIL test
            steps {
                sh '$PY -m pytest --junitxml=reports/tests.xml'
            }
        }

        stage('Traceability') {
            // Gate: every active requirement must be verified by a passing test
            steps {
                sh '$PY -m tools.trace_report'
            }
        }

        stage('Package release') {
            steps {
                sh '$PY -m tools.package_release'
            }
        }
    }

    post {
        always {
            // If tests failed, still build the matrix so the failure is visible per requirement
            sh '[ -f reports/req_results.json ] && [ ! -f reports/traceability.html ] && $PY -m tools.trace_report || true'
            junit allowEmptyResults: true, testResults: 'reports/tests.xml'
            archiveArtifacts allowEmptyArchive: true,
                artifacts: 'reports/**, dist/*.zip, controller/build/libmotorctrl.so'
        }
    }
}
