# GEEKROS UNITREE PACKAGE

⚡ Unitree Package For Real-Time Robot-Human Interaction Applications ⚡

## License

[![License:Apache2.0](https://img.shields.io/badge/License-Apache2.0-yellow.svg)](https://opensource.org/licenses/Apache2.0)

## Basic Requirements

Your device should meet the following basic requirements.

```shell
Distributor ID: Ubuntu
Description:    Ubuntu 20.04 LTS、Ubuntu 22.04 LTS、Ubuntu 24.04 LTS
```

## Install

```shell
git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x
cd cyclonedds && mkdir build install && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=../install
cmake --build . --target install

sudo env "CYCLONEDDS_HOME=$HOME/cyclonedds/install" "CMAKE_PREFIX_PATH=$HOME/cyclonedds/install" pip3 install -e .
```
