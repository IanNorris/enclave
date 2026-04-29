# Mimir runtime binaries

This directory holds host-built Mimir binaries that the agent container
COPYs in at image-build time. The binaries are gitignored — they're
~30-50 MB each, platform-specific, and would change with every Mimir
upstream commit.

## Local build

```bash
cd ~/Projects/Mimir-vendor/Mimir
cargo build --release -p mimir-mcp -p mimir-cli -p mimir-librarian
cp target/release/mimir-{mcp,cli,librarian} \
   <enclave>/container/mimir-bin/
```

## Production / CI

Replace this directory's contents with a `curl | tar` of pinned release
artifacts once Mimir cuts versioned releases.
