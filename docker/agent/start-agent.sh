#!/bin/sh
set -eu

token="$(cat /run/secrets/jenkins_api_token)"
jnlp_url="${JENKINS_URL%/}/computer/${JENKINS_AGENT_NAME}/jenkins-agent.jnlp"
attempt=0
while [ "$attempt" -lt 60 ]; do
  document="$(curl --fail --location --silent --user "jenkinsservice:${token}" "$jnlp_url" || true)"
  secret="$(printf '%s' "$document" | sed -n 's:.*<argument>\([a-f0-9]\{64\}\)</argument>.*:\1:p' | head -n 1)"
  if [ -n "$secret" ]; then
    exec /usr/local/bin/jenkins-agent \
      -secret "$secret" \
      -workDir /home/jenkins/agent
  fi
  attempt=$((attempt + 1))
  sleep 2
done
echo "unable to obtain Jenkins inbound-agent secret" >&2
exit 1
