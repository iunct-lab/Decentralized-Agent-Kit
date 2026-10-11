import os

# litellm fetches its model map (context windows, prices) from the network at
# import unless told otherwise, so an upstream edit could fail these tests
# with no change here (#618). Use the map shipped with the locked litellm.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
