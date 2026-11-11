"""Behavior evals: does a chat turn reach the right bot, skill and model, and does the server
handle what the model writes (approval lines, consults, reactions) the way the rules say?

Offline evals (evals.turns, evals.recall) run a real turn through the server with a scripted
engine: free, deterministic, part of the test suite. Live evals (evals.live) send a few
known-answer prompts to a real engine on request, show the cost and compare with the last run.
Fixtures use generic slugs only (acme, demo).
"""
