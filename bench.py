"""Render 100k messages per locale and report timings."""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msgfmt import MessageFormatter

N = 100_000


def main():
    fmt = MessageFormatter.from_dir(
        os.path.join("config", "languages.json"), "messages")
    args = {"user": "Ada", "count": 5, "total": 1234.5,
            "due": datetime.date(2026, 9, 30)}

    # Warm-up + correctness check.
    expected = {
        "en": "Hi Ada, you have 5 items totaling 1,234.5, due on 09/30/2026.",
        "ru": "Ada, у вас 5 товаров на сумму 1 234,5, оплатить до 30.09.2026.",
        "ja": "Ada さん：2026/09/30 までに合計 1,234.5、5 点 の商品があります。",
    }
    for locale, text in expected.items():
        assert fmt.render(locale, "cart.summary", args).text == text

    print(f"renders per locale: {N:,}  (python {sys.version.split()[0]})")
    for locale in expected:
        start = time.perf_counter()
        for _ in range(N):
            fmt.render(locale, "cart.summary", args)
        elapsed = time.perf_counter() - start
        print(f"  {locale}: {elapsed:.3f}s total, "
              f"{elapsed / N * 1e6:.2f} µs/render, "
              f"{N / elapsed:,.0f} renders/s")


if __name__ == "__main__":
    main()
