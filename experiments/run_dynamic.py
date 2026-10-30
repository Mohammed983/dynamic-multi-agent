"""Use --topology heuristic or --topology mlp --checkpoint PATH."""
from .run_baseline import main

if __name__ == '__main__':
    main(default_topology='heuristic')
