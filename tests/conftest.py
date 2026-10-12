import os, tempfile
_d = tempfile.mkdtemp(prefix="btcdash-test-")
os.environ["DATA_DIR"] = _d
os.environ["LOAD_EXAMPLE_DATA"] = "0"
os.environ["BTC_DASH_NO_SCHEDULER"] = "1"
os.environ["APP_ELECTRS_NODE_IP"] = "10.21.21.10"
os.environ["APP_ELECTRS_NODE_PORT"] = "50001"
os.environ.pop("STRIKE_API_KEY", None)
os.environ.pop("TRUSTED_PROXY_IPS", None)
