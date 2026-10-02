"""Typed failures shared across resolver workflow modules."""

class PacketDependencyCycle(ValueError):
    pass

class PacketDependencyError(ValueError):
    pass

class PacketValidationError(PacketDependencyError):
    pass
