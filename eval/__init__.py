"""Eval harness: measures extraction accuracy, flag quality, cost and latency.

    python -m eval.run extract      # calls real Gemini once per fixture, saves raw results
    python -m eval.run score latest # pure + offline: re-score saved results for free
"""
