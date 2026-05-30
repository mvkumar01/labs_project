"""
live/brokers/ — the ONLY package in the repo permitted to import a broker
order SDK (`kiteconnect`, `SmartApi`). The §11 CI grep gate fails the build
if `import kiteconnect` / `from SmartApi` appears outside this directory.
"""
