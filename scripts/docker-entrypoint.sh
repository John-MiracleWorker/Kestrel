#!/bin/sh
set -eu

# Initialize missing Memvid v2 layers before admitting traffic. `nest-agent
# init` opens existing .mv2 files and creates only missing layers; corruption
# or configuration errors therefore stop the container instead of being hidden.
if [ "$#" -ge 2 ] && [ "$1" = "nest-agent" ] && [ "$2" = "server" ]; then
  nest-agent init
fi

exec "$@"
