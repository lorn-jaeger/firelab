from pathlib import Path
import pandas as pd
import argparse
import warnings
from ee.geometry import Geometry
from ee.imagecollection import ImageCollection
from tqdm import tqdm

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import geemap

SENSOR_PATH = Path("data/sensors")
TEST_PATH = Path("data/test/sensors")
OUT_PATH = Path("data/out")

def clear_output():
    """
    Clear the csv output.
    """
    for file in OUT_PATH.iterdir():
        if file.is_file():
            file.unlink()


def parse_args():
    """
    Simple argument parser with some useful commands.
    Allows me to set a test dir and clear the output.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear-out", action="store_true")
    parser.add_argument("--test", action="store_true")

    args = parser.parse_args()

    if args.clear_out:
        clear_output() 

    global SENSOR_PATH

    if args.test:
        SENSOR_PATH = TEST_PATH

count = 0

def save(file):
    global count
    file.to_csv(f"data/out/out{count}", index=False)
    count += 1

def try_auth():
    geemap.ee_initialize()

def fmt_sensors(sensors):
    sensors['Date GMT'] = pd.to_datetime(sensors['Date GMT'])
    sensors['Time'] = sensors['Date GMT'] + pd.to_timedelta(sensors['Time GMT'] + ':00')
    
    sensors = sensors[["Time", "Latitude", "Longitude", "Sample Measurement"]]

    return sensors

def get_unique(sensors):
    result = sensors.groupby(['Latitude', 'Longitude'])['Time'].agg(['min', 'max']).reset_index()
    result.rename(columns={'min': 'Start Time', 'max': 'End Time'}, inplace=True)

    return result


def CONUS(sensors):
    sensors["CONUS"] = pd.NA
    return sensors



from earthaccess import login, search_data, download
import xarray as xr
import pandas as pd
from tqdm import tqdm
from ee.geometry import Geometry


from earthaccess import search_data, download
import xarray as xr
import pandas as pd
from tqdm import tqdm
from pathlib import Path

def MERRA2(sensors):
    sensors["MERRA2"] = pd.NA
    variable = "MERRA2_CNN_Surface_PM25"
    data = pd.DataFrame(columns=["Time", "Latitude", "Longitude", "pm25"])

    locations = get_unique(sensors)

    for _, row in locations.iterrows():
        lat, lon = row["Latitude"], row["Longitude"]
        start = pd.to_datetime(row["Start Time"])
        end = pd.to_datetime(row["End Time"])
        print(f"Processing sensor at {lat}, {lon} from {start} to {end}")

        results = search_data(
            concept_id="C3094710982-GES_DISC",
            temporal=(start, end),
            bounding_box=(lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1),
        )
        files = download(results)
        if not files:
            continue

        try:
            ds = xr.open_mfdataset(files, combine="by_coords")
            times = pd.date_range(start, end, freq="H")
            sat_pm25 = ds[variable].interp(
                time=xr.DataArray(times, dims="time"),
                lat=lat,
                lon=lon
            )
            df = pd.DataFrame({
                "Time": times,
                "Latitude": lat,
                "Longitude": lon,
                "pm25": sat_pm25.values
            })
            data = pd.concat([data, df], ignore_index=True)
        except Exception as e:
            print(f"Failed {lat},{lon}: {e}")
        finally:
            ds.close()
            for f in files:
                try:
                    Path(f).unlink()
                except Exception as e:
                    print(f"Error deleting file {f}: {e}")

    # Round to avoid floating-point merge mismatch
    data["Latitude"] = data["Latitude"].round(4)
    data["Longitude"] = data["Longitude"].round(4)
    sensors["Latitude"] = sensors["Latitude"].round(4)
    sensors["Longitude"] = sensors["Longitude"].round(4)

    sensors = sensors.merge(data, on=["Time", "Latitude", "Longitude"], how="left")
    sensors["MERRA2"] = sensors["pm25"]
    sensors.drop(columns=["pm25"], inplace=True)

    print(sensors)
    print(sensors.shape)

    return sensors



def MERRA2R(sensors):
    sensors["MEERA2R"] = pd.NA
    return sensors

def AIRNOW(sensors):
    sensors["AIRNOW"] = pd.NA
    return sensors


def parse_gee_region(raw):
    headers = raw[0]
    data = raw[1:]

    df = pd.DataFrame(data, columns=headers)

    df = df[df["time"].apply(lambda x: isinstance(x, (int, float)))]

    df["Time"] = pd.to_datetime(df["time"], unit="ms")
    df["Longitude"] = df["longitude"].astype(float)
    df["Latitude"] = df["latitude"].astype(float)
    df["pm25"] = df["particulate_matter_d_less_than_25_um_surface"].astype(float)

    df["pm25"] *= 1_000_000_000

    return df[["Time", "Longitude", "Latitude", "pm25"]]

def CAMS(sensors):
    sensors["CAMS"] = pd.NA

    cams = (
        ImageCollection("ECMWF/CAMS/NRT")
        .select("particulate_matter_d_less_than_25_um_surface")
        .filter('model_initialization_hour == 0')
    )

    cams_data = pd.DataFrame(columns=["Time", "Longitude", "Latitude", "pm25"]) #type: ignore

    locations = get_unique(sensors)
    outer = tqdm(locations.iterrows(), total=len(locations), desc="Fetching CAMS data", position=0)
    # can use apply instead but this is only 1000 iterations
    # most of the time is taken by api calls to earth engine
    for _, row in outer:
        point = Geometry.Point(row["Longitude"], row["Latitude"])
        # batching is needed so we don't hit the 3000 item limit
        months = pd.date_range(pd.to_datetime(row["Start Time"]), pd.to_datetime(row["End Time"]), freq="ME")
        inner = tqdm(months, desc="Monthly data", leave=False, position=1)
        for month in inner:
            start = month
            end = month + pd.DateOffset(months=1)

            # skip if start is before the start of the dataset
            if start < pd.Timestamp("2016-06-23"):
                continue

            try:

                monthly_data = (
                    cams
                    .filterBounds(point)
                    .filterDate(start, end)
                    .getRegion(point, scale=10_000)
                    .getInfo()
                )
                monthly_data = parse_gee_region(monthly_data)
                # Needed overwrite. Google Earth Engine rounds lats and lons so there are slightly off
                # Otherwise there will be no matches on merge
                monthly_data["Latitude"] = row["Latitude"]
                monthly_data["Longitude"] = row["Longitude"]
                cams_data = pd.concat([cams_data, monthly_data], ignore_index=True) #type: ignore
            except Exception as e:
                print(e)
                continue
            

    sensors = sensors.merge(
        cams_data,
        on=["Time", "Latitude", "Longitude"],
        how="left"
    )

    sensors["CAMS"] = sensors["pm25"]
    sensors.drop(columns=["pm25"], inplace=True)

    return sensors

def main() -> None:
    parse_args()
    try_auth()

    files = SENSOR_PATH.iterdir()
    sources = [MERRA2]

    for file in files:
        print(f"Reading {file.name}")
        sensors = pd.read_csv(file, low_memory=False)
        for source in sources:
            sensors = fmt_sensors(sensors)
            sensors = source(sensors)
            print(sensors)
        save(sensors)

if __name__ == "__main__":
    main()



