"""Aggregate records without silently combining simulated and real runs."""
import argparse
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+')
    args = parser.parse_args()
    frame = pd.concat([pd.read_json(path, lines=True) for path in args.files], ignore_index=True)
    frame['method'] = frame['config'].map(lambda c: c['topology'])
    grouped = frame.groupby(['simulated', 'model', 'benchmark', 'split', 'method'], dropna=False)
    print(grouped.agg(questions=('item_id', 'count'), accuracy=('correct', 'mean'),
                      input_tokens=('input_tokens', 'sum'), output_tokens=('output_tokens', 'sum'),
                      total_tokens=('total_tokens', 'sum'), latency=('latency', 'sum')).to_string())


if __name__ == '__main__':
    main()
