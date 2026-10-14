"""Fixed toolkit for placing report segments on a map.

Every generic step (report inspection, data download, terrain modelling,
georeferencing, deliverables) is a CLI command. Run-specific judgement lives
in data files inside the run folder (run.json, scripts/placements.json), so a
model running the mapping prompt never has to write code.
"""

__version__ = "1.0.0"
