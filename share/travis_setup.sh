#!/bin/bash
set -evx

mkdir ~/.absolutecore

# safety check
if [ ! -f ~/.absolutecore/.absolute.conf ]; then
  cp share/absolute.conf.example ~/.absolutecore/absolute.conf
fi
