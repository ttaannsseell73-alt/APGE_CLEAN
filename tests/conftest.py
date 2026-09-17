from apge.testnet_runtime import TestnetRuntime

# Prevent pytest from treating the production runtime class as a test class
# when test modules import it into their namespace.
TestnetRuntime.__test__ = False
