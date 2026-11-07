# Local Jenkins controller that can start Docker build agents.
# Adds the Docker CLI (it talks to your machine's Docker via the mounted socket)
# and pre-installs the Docker Pipeline plugin the Jenkinsfile needs.
FROM jenkins/jenkins:lts-jdk17

USER root
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
USER jenkins

RUN jenkins-plugin-cli --plugins docker-workflow
