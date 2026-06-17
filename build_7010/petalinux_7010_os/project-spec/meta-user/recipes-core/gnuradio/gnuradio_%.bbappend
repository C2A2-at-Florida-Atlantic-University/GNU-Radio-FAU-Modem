# Make the TARGET Python (3.12) sysconfig authoritative during cross-compile.
# Root fix for the cpython-310 ABI tag on a 3.12 target.
inherit python3targetconfig

# Headless embedded ARM: no desktop OpenGL (gr-qtgui uses fixed-function desktop GL)
EXTRA_OECMAKE:append = " -DENABLE_GR_QTGUI=OFF"
