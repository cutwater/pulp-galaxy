#!/usr/bin/env bash
# coding=utf-8

set -mveuo pipefail

# Aliases for running commands in the pulp-api container.
export PULP_API_POD=$(sudo kubectl get pods | grep -E -o "pulp-api-(\w+)-(\w+)")
# Run a command, and pass STDIN
export CMD_STDIN_PREFIX="sudo kubectl exec -i $PULP_API_POD --"


sudo kubectl cp ../pulp-openapi-generator/pulp_galaxy-client $PULP_API_POD:/tmp
$CMD_STDIN_PREFIX bash -c "cd /tmp && pip install ./pulp_galaxy-client"


pytest -v -r sx --color=yes --pyargs pulp_galaxy.tests.functional || show_logs_and_return_non_zero
