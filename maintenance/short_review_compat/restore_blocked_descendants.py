"""Retired scientific-review recovery entry; fail closed."""
def run(*args, **kwargs):
    raise RuntimeError("Retired: scientific review and review-gated recovery are prohibited; use compute-only scheduling")

if __name__ == "__main__":
    run()
