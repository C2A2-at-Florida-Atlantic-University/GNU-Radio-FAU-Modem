# Make the TARGET Python (3.12) sysconfig authoritative during cross-compile.
# Root fix for cpython-310 ABI tag on a 3.12 target.
inherit python3targetconfig

# Turn off the graphical rendering module to accommodate headless embedded ARM constraints
EXTRA_OECMAKE:append = " -DENABLE_GR_QTGUI=OFF"

# Inline patch injection to automatically neutralize PyEval_InitThreads for Python 3.12 compliance
do_configure:prepend() {
    if [ -f ${S}/gr-python/lib/python.cc ]; then
        sed -i 's/PyEval_InitThreads();/\/\/ PyEval_InitThreads(); removed for Python 3.12 compatibility/g' ${S}/gr-python/lib/python.cc
    fi
}
