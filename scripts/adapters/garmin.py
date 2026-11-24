"""Garmin Connect CSV activity and daily summary fields."""

from .tabular import column_map, csv_header, csv_rows, inspect_table, mapped_rows


COLUMNS = column_map(
    ("steps", "count", "steps stepcount"),
    ("distance", "km", "distance distancekm distancewalkrun"),
    ("distance", "mi", "distancemi"),
    ("active_energy", "kcal", "activecalories activecalorieskcal"),
    ("basal_energy", "kcal", "restingcalories"),
    ("workout_minutes", "min", "time duration workoutduration"),
    ("sleep", "h", "sleep totalsleep totalsleeptime sleepduration"),
    ("sleep", "min", "sleepminutes"),
    ("heart_rate", "bpm", "heartrate hr avghr averageheartrate"),
    ("resting_hr", "bpm", "restingheartrate restinghr"),
    ("body_mass", "kg", "weight weightkg"),
    ("body_mass", "lb", "weightlbs"),
    ("oxygen_saturation", "%", "bloodoxygen spo2"),
)


def inspect(path):
    return inspect_table(csv_header(path), COLUMNS)


def iter_samples(path):
    yield from mapped_rows(csv_rows(path), COLUMNS, "garmin")
