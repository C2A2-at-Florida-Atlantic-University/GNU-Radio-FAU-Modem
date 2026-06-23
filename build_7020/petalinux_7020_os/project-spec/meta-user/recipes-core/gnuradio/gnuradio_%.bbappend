inherit python3targetconfig

PACKAGECONFIG = "zeromq"
PACKAGECONFIG:remove = "qtgui5 grc"

DEPENDS:append = " python3-pybind11"
EXTRA_OECMAKE:append = " -DENABLE_PYTHON=ON -DPYBIND11_FINDPYTHON=ON \
    -Dpybind11_DIR=${RECIPE_SYSROOT}/usr/lib/python3.12/site-packages/pybind11/share/cmake/pybind11"