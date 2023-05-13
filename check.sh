set -ex
./pretty.sh
autopep8 -r --diff .
mypy .
