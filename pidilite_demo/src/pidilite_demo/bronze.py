import dlt
from pyspark.sql.functions import current_timestamp, col

LANDING_ROOT = "/Volumes/pidilite_demo/bronze/landing"

# one Auto Loader stream per entity - each source folder is its own
# checkpoint/schema-location, no transformation, everything lands as STRING
ENTITIES = ["division", "person", "field_team", "customer", "sales"]


def _make_bronze_table(entity: str):
    @dlt.table(
        name=f"pidilite_demo.bronze.raw_{entity}",
        comment=f"Bronze: raw {entity} rows landed as-is via Auto Loader, no transformation.",
    )
    def _bronze():
        return (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "csv")
            .option("cloudFiles.inferColumnTypes", "false")  # keep everything STRING
            .option("header", "true")
            .load(f"{LANDING_ROOT}/{entity}/")
            .withColumn("_ingested_at", current_timestamp())
            .withColumn("_source_file", col("_metadata.file_path"))
        )

    return _bronze


for _entity in ENTITIES:
    _make_bronze_table(_entity)
