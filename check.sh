set -ex
./pretty.sh
autopep8 -r --diff .
python -m mypy .
