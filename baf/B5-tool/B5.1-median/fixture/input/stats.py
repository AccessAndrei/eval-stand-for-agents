"""Вычислить медиану столбца value в CSV."""

import csv
import statistics
import sys
from decimal import Decimal
from pathlib import Path


def main() -> None:
    with Path(sys.argv[1]).open(encoding="utf-8", newline="") as stream:
        values = [Decimal(row["value"]) for row in csv.DictReader(stream)]
    print(statistics.median(values))


if __name__ == "__main__":
    main()
