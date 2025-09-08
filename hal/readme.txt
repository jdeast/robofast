The Hardware Abstraction Layers (HAL) live in this directory.

The names of these files should match the "hal_mod", and the class name should
match the "hal_class" in the corresponding config file.

The goal is to make these low-level hardware layers and the config files the
only things that change between observatories. Everything else is handled
transparently by ROBOFAST.
