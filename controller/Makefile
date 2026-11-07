# Builds the controller as a shared library for software-in-the-loop testing.
# Strict flags on purpose: embedded code should compile warning-free.

CC     ?= gcc
CFLAGS  = -std=c99 -O2 -fPIC -fvisibility=hidden \
          -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wdouble-promotion -Werror \
          -Iinclude
SRC     = src/motor_ctrl.c platform/sil_api.c

build/libmotorctrl.so: $(SRC) include/motor_ctrl.h
	mkdir -p build
	$(CC) $(CFLAGS) -shared -o $@ $(SRC)

clean:
	rm -rf build

.PHONY: clean
