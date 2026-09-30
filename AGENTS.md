# Project instructions

- The application has a single user: its owner, who is in full control of the runtime environment.
- Backwards compatibility is never a concern. Do not add compatibility layers, legacy endpoint support, migrations solely for compatibility, or support for older clients or servers. Update the affected components together.
- Do not add fallbacks or graceful degradation to preserve functionality when dependencies, configuration, or the environment break. Let the failure surface so the owner can debug and fix it.
- Ordinary errors and tracebacks from the failing operation are sufficient. Do not add verbose diagnostic messaging, troubleshooting guidance, or error-handling machinery to babysit the user.
- These rules do not prohibit error handling needed for correctness, resource cleanup, or security.
