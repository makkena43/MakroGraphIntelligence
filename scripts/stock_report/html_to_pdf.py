#!/usr/bin/env python3
"""Convert a report HTML file to PDF using headless Chrome.

Usage: python scripts/stock_report/html_to_pdf.py report.html [report.pdf]
"""
import os
import subprocess
import sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def main():
    if len(sys.argv) < 2:
        print("usage: html_to_pdf.py <input.html> [output.pdf]", file=sys.stderr)
        sys.exit(1)
    html = os.path.abspath(sys.argv[1])
    pdf = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.splitext(html)[0] + ".pdf"
    if not os.path.exists(CHROME):
        print(f"Chrome not found at {CHROME}", file=sys.stderr)
        sys.exit(2)
    cmd = [
        CHROME, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--virtual-time-budget=4000",
        f"--print-to-pdf={pdf}", f"file://{html}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(pdf):
        print(res.stderr[-2000:], file=sys.stderr)
        sys.exit(3)
    print(pdf)


if __name__ == "__main__":
    main()
