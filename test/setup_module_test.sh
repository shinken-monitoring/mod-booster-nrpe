#!/bin/bash

set -e

SHINKEN_DEST=${SHINKEN_DEST-"$HOME/tmp_shinken"}

py=$(python -c "import sys; print(''.join(map(str, sys.version_info[:2])))")

get_name (){
    echo $(python -c 'import json; print json.load(open("'$1'package.json"))["name"]')
}

setup_submodule (){
    for dep in $(cat test/dep_modules.txt); do
        mname=$(basename $dep | sed 's/.git//g')
        git clone $dep ~/$mname
        rmname=$(get_name ~/$mname/)
        cp -r  ~/$mname/module "$SHINKEN_DEST/modules/$rmname"
        if [ -f ~/$mname/requirements.txt ]
        then
            pip install -r ~/$mname/requirements.txt
        fi
    done
}

install_if_exist() {
    if [ -f "$1" ]
    then
        pip install -r "$1"
    fi
}

name=$(get_name)

# install and setup (requirements) shinken in $SHINKEN_DEST:
test -e "$SHINKEN_DEST" && rm -rf "$SHINKEN_DEST"
git clone --depth 5 https://github.com/naparuba/shinken.git "$SHINKEN_DEST"
install_if_exist "$SHINKEN_DEST/requirements.txt"

# install our eventual submodules:
[ -f test/dep_modules.txt ] && setup_submodule

# install our own requirements :
install_if_exist "requirements.txt"
install_if_exist "test-requirements.txt"
[ "$py" == "26" ] && install_if_exist "test-requirements-py26.txt"

# to protect against previous test
true
