# Operations Nerd

A proof-of-concept ops automation system for small businesses.

It takes an inbound event (like a customer email), drafts a response using an
LLM, and routes it through a human approval step before anything is sent.

The core idea being tested: the same engine should work across completely
different industries — real estate, a health club, etc. — just by swapping
out a config file, with no changes to the underlying code.

Currently supports two example industries: **Real Estate** and **Health Club**.
