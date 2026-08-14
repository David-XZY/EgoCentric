#!/bin/sh
export GI_TYPELIB_PATH="/usr/lib/x86_64-linux-gnu/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
exec /opt/egocentric-capture/egocentric-capture "$@"
