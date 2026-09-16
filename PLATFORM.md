# Platform support

The beta runtime is POSIX-oriented and tested on Linux. It relies on POSIX
file permissions, advisory file locks, SQLite WAL, and atomic rename. Windows
is unsupported for this release; a future port must replace those contracts
before advertising cross-platform installation.
