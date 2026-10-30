"""Train only from explicitly collected paired intervention data."""
import argparse
import hashlib
import json
from pathlib import Path
from src.training import EdgeTrainingExample, train_controller


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('examples', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--hidden-dims', type=int, nargs='+', default=[32, 16])
    args = parser.parse_args()
    rows = [EdgeTrainingExample(**json.loads(line)) for line in args.examples.read_text().splitlines() if line.strip()]
    controller, report = train_controller(rows, tuple(args.hidden_dims), args.epochs, seed=args.seed)
    report['examples_sha256'] = hashlib.sha256(args.examples.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    controller.save(args.output)
    args.output.with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
