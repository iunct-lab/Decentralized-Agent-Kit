---
name: x402
description: "Fetch external HTTP resources. When a resource answers HTTP 402 (x402), report the payment requirement instead of paying."
tools:
  - fetch_url
---
You can fetch external HTTP resources with `fetch_url(url)`.

# Paid resources (x402)
Some resources answer HTTP 402 Payment Required. `fetch_url` then returns the payment requirement
(amount, asset, network, payTo, expires_in) and nothing is paid or signed.
1. Show the requirement to the user as returned
2. Whether to pay is decided by the user's approval, not by you
3. Do NOT suggest paying on your own initiative
