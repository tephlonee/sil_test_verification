// Jenkinsfile - CI pipeline for the motor controller SIL rig.
// Needs the "Docker Pipeline" plugin. The agent image (ci/Dockerfile) adds cppcheck
// to python:3.12, which already ships gcc and make.

pipeline {
    agent { dockerfile { filename 'ci/Dockerfile' } }

    environment {
        MPLBACKEND = 'Agg'          // plots render headless on the build agent
        PY         = '.venv/bin/python'
    }

    options {
        timeout(time: 15, unit: 'MINUTES')
    }

    stages {
        stage('Setup') {
            steps {
                sh 'python -m venv .venv && $PY -m pip install --quiet -r requirements.txt'
                sh 'mkdir -p reports'
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

        stage('Plant model tests') {
            steps {
                sh '$PY -m pytest test_plant.py --junitxml=reports/plant-tests.xml'
            }
        }

        stage('Closed-loop SIL run') {
            steps {
                sh '$PY run_closed_loop.py | tee reports/closed_loop.txt'
                // Smoke check: the fault scenario must end in SAFE with torque at zero
                sh 'grep -q "Final mode SAFE" reports/closed_loop.txt'
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/*.xml'
            archiveArtifacts allowEmptyArchive: true,
                artifacts: 'controller/build/libmotorctrl.so, *.png, reports/**'
        }
    }
}
