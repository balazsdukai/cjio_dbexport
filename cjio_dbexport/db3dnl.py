# -*- coding: utf-8 -*-
"""Export from the 3DNL database.

Copyright (c) 2019, 3D geoinformation group, Delft University of Technology

The MIT License (MIT)

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import logging
import re
from datetime import datetime
from typing import Mapping, Sequence, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

from click import ClickException, echo
from cjio import cityjson
from cjio.models import CityObject, Geometry
from psycopg2 import sql, pool, Error as pgError

from cjio_dbexport import db, utils

log = logging.getLogger(__name__)


def build_query(conn: db.Db, features: db.Schema, tile_index: db.Schema,
                tile_list=None, bbox=None, extent=None):
    """Build an SQL query for extracting CityObjects from a single table.

    ..todo: make EPSG a parameter
    """
    # Set EPSG
    epsg = 7415
    # Exclude columns from the selection
    table_fields = conn.get_fields(features.schema + features.table)
    if features.field.exclude:
        exclude = [f.string for f in features.field.exclude if f is not None]
    else:
        exclude = []
    attr_select = sql.SQL(', ').join(sql.Identifier(col) for col in table_fields
                                     if col != features.field.pk.string and
                                     col not in features.field.geometry.values() and
                                     col != features.field.cityobject_id.string and
                                     col not in exclude)
    lods = list(features.field.geometry.keys())
    # polygons subquery
    if bbox:
        log.info(f"Exporting with BBOX {bbox}")
        polygons_sub, attr_where, extent_sub = query_bbox(features, bbox, epsg,
                                                          lod=lods[0])
    elif tile_list:
        log.info(f"Exporting with a list of tiles {tile_list}")
        polygons_sub, attr_where, extent_sub = query_tiles_in_list(
            features=features,
            tile_index=tile_index,
            tile_list=tile_list,
            lod=lods[0]
        )
    elif extent:
        log.info(f"Exporting with polygon extent")
        ewkt = utils.to_ewkt(polygon=extent, srid=epsg)
        polygons_sub, attr_where, extent_sub = query_extent(features=features,
                                                            ewkt=ewkt,
                                                            lod=lods[0])
    else:
        log.info(f"Exporting the whole database")
        polygons_sub, attr_where, extent_sub = query_all(features=features,
                                                         lod=lods[0])

    # Main query
    query_params = {
        'pk': features.field.pk.sqlid,
        'coid': features.field.cityobject_id.sqlid,
        'tbl': features.schema + features.table,
        'attr': attr_select,
        'where_instersects': attr_where,
        'extent': extent_sub,
        'query_geometry': query_geometry(
            conn=conn, features=features, polygons_sub=polygons_sub,
            extent_sub=extent_sub, lod=lods[0]
        )
    }

    query = sql.SQL("""
    WITH
         {extent}
         attr_in_extent AS (
             SELECT {pk} pk,
                    {coid} coid,
                    {attr}
             FROM {tbl} a
             {where_instersects}),
         multisurfaces AS ({query_geometry})
    SELECT *
    FROM multisurfaces b
             INNER JOIN attr_in_extent a ON
        b.pk = a.pk;
    """).format(**query_params)
    log.debug(conn.print_query(query))
    return query

def query_geometry(conn: db.Db, features: db.Schema,
                   polygons_sub: sql.Composed, extent_sub: sql.Composed,
                   lod: str):
    """Parse the PostGIS geometry representation into
    a CityJSON-like geometry array representation. Here I use
    several subqueries for sequentially aggregating the vertices,
    rings and surfaces. I also tested the aggregation with window
    function calls, but this approach tends to be at least twice
    as expensive then the subquery-aggregation.
    In the expand_point subquery, the first vertex is skipped,
    because PostGIS uses Simple Features so the first vertex is
    duplicated at the end.
    """
    # Main query
    query_params = {
        'pk': features.field.pk.sqlid,
        'coid': features.field.cityobject_id.sqlid,
        'geometry': getattr(features.field.geometry, lod).sqlid,
        'tbl': features.schema + features.table,
        'polygons': polygons_sub,
        'extent': extent_sub,
    }

    query = sql.SQL("""
    WITH
         {extent}
         {polygons},
         expand_points AS (
             SELECT pk,
                    (geom).PATH[1]         exterior,
                    (geom).PATH[2]         interior,
                    (geom).PATH[3]         vertex,
                    ARRAY [ST_X((geom).geom),
                        ST_Y((geom).geom),
                        ST_Z((geom).geom)] point
             FROM polygons
             WHERE (geom).PATH[3] > 1
             ORDER BY pk,
                      exterior,
                      interior,
                      vertex),
         rings AS (
             SELECT pk,
                    exterior,
                    interior,
                    ARRAY_AGG(point) geom
             FROM expand_points
             GROUP BY interior,
                      exterior,
                      pk
             ORDER BY exterior,
                      interior),
         surfaces AS (
             SELECT pk,
                    ARRAY_AGG(geom) geom
             FROM rings
             GROUP BY exterior,
                      pk
             ORDER BY exterior),
         multisurfaces AS (
             SELECT pk,
                    ARRAY_AGG(geom) geom
             FROM surfaces
             GROUP BY pk)
    SELECT b.pk,
           b.geom
    FROM multisurfaces b
    """).format(**query_params)
    log.debug(conn.print_query(query))
    return query


def query_all(features, lod: str) -> Tuple[sql.Composed, ...]:
    """Build a subquery of all the geometry in the table."""
    query_params = {
        'pk': features.field.pk.sqlid,
        'coid': features.field.cityobject_id.sqlid,
        'geometry': getattr(features.field.geometry, lod).sqlid,
        'tbl': features.schema + features.table
    }
    sql_polygons =  sql.SQL("""
    polygons AS (
        SELECT
            {pk}                      pk,
            ST_DumpPoints({geometry}) geom
        FROM 
            {tbl}
    )
    """).format(**query_params)
    sql_where_attr_intersects = sql.Composed("")
    sql_extent = sql.Composed("")
    return sql_polygons, sql_where_attr_intersects, sql_extent


def query_bbox(features: db.Schema, bbox: Sequence[float], epsg: int, lod: str) -> Tuple[sql.Composed, ...]:
    """Build a subquery of the geometry in a BBOX."""
    query_params = {
        'pk': features.field.pk.sqlid,
        'coid': features.field.cityobject_id.sqlid,
        'geometry': getattr(features.field.geometry, lod).sqlid,
        'epsg': sql.Literal(epsg),
        'xmin': sql.Literal(bbox[0]),
        'ymin': sql.Literal(bbox[1]),
        'xmax': sql.Literal(bbox[2]),
        'ymax': sql.Literal(bbox[3]),
        'tbl': features.schema + features.table
    }
    sql_polygons = sql.SQL("""
    polygons AS (
        SELECT {pk}                      pk,
               ST_DumpPoints({geometry}) geom
        FROM
            {tbl}
        WHERE ST_Intersects(
            {geometry},
            ST_MakeEnvelope({xmin}, {ymin}, {xmax}, {ymax}, {epsg})
            )
    )
    """).format(**query_params)
    sql_where_attr_intersects = sql.SQL("""
    WHERE ST_Intersects(
        a.{geometry},
        ST_MakeEnvelope({xmin}, {ymin}, {xmax}, {ymax}, {epsg})
        )
    """).format(**query_params)
    sql_extent = sql.Composed("")
    return sql_polygons, sql_where_attr_intersects, sql_extent


def query_extent(features: db.Schema, ewkt: str, lod: str) -> Tuple[sql.Composed, ...]:
    """Build a subquery of the geometry in a polygon."""
    query_params = {
        'pk': features.field.pk.sqlid,
        'coid': features.field.cityobject_id.sqlid,
        'geometry': getattr(features.field.geometry, lod).sqlid,
        'tbl': features.schema + features.table,
        'poly': sql.Literal(ewkt),
    }
    sql_polygons = sql.SQL("""
    polygons AS (
        SELECT
            {pk}                      pk,
            ST_DumpPoints({geometry}) geom
        FROM
            {tbl}
        WHERE ST_Intersects(
            {geometry},
            {poly}
            )
    )
    """).format(**query_params)
    sql_where_attr_intersects = sql.SQL("""
    WHERE ST_Intersects(
        a.{geometry},
        {poly}
        )
    """).format(**query_params)
    sql_extent = sql.Composed("")
    return sql_polygons, sql_where_attr_intersects, sql_extent


def query_tiles_in_list(features: db.Schema, tile_index:db.Schema,
                        tile_list: Sequence[str], lod: str
                        ) -> Tuple[sql.Composed, ...]:
    """Build a subquery of the geometry in the tile list."""
    tl_tup = tuple(tile_list)
    query_params = {
        'tbl': features.schema + features.table,
        'tbl_pk': features.field.pk.sqlid,
        'tbl_coid': features.field.cityobject_id.sqlid,
        'tbl_geom': getattr(features.field.geometry, lod).sqlid,
        'tile_index': tile_index.schema + tile_index.table,
        'tx_geom': tile_index.field.geometry.sqlid,
        'tx_pk': tile_index.field.pk.sqlid,
        'tile_list': sql.Literal(tl_tup)
    }
    sql_extent = sql.SQL("""
    extent AS (
        SELECT ST_Union({tx_geom}) geom
        FROM {tile_index}
        WHERE {tx_pk} IN {tile_list}),
    """).format(**query_params)
    sql_polygon = sql.SQL("""
    geom_in_extent AS (
        SELECT a.*
        FROM {tbl} a,
            extent t
        WHERE ST_Intersects(t.geom,
                            a.{tbl_geom})),
    polygons AS (
        SELECT {tbl_pk}                  pk,
            ST_DumpPoints({tbl_geom}) geom
        FROM geom_in_extent b)
    """).format(**query_params)
    sql_where_attr_intersects = sql.SQL("""
    ,extent t WHERE ST_Intersects(t.geom, a.{tbl_geom})
    """).format(**query_params)
    return sql_polygon, sql_where_attr_intersects, sql_extent


def with_list(conn: db.Db, tile_index: db.Schema,
              tile_list: Tuple[str]) -> List[str]:
    """Select tiles based on a list of tile IDs."""
    if 'all' == tile_list[0].lower():
        log.info("Getting all tiles from the index.")
        in_index = all_in_index(conn=conn, tile_index=tile_index)
    else:
        log.info("Verifying if the provided tiles are in the index.")
        in_index = tiles_in_index(conn=conn, tile_index=tile_index,
                                  tile_list=tile_list)
    if len(in_index) == 0:
        raise AttributeError("None of the provided tiles are present in the"
                             " index.")
    else:
        return in_index


def tiles_in_index(conn: db.Db, tile_index: db.Schema,
                   tile_list: Tuple[str]) -> Tuple[List[str], List[str]]:
    """Return the tile IDs that are present in the tile index."""
    if not isinstance(tile_list, tuple):
        tile_list = tuple(tile_list)
        log.debug(f"tile_list was not a tuple, casted to tuple {tile_list}")

    query_params = {
        'tiles': sql.Literal(tile_list),
        'index_': tile_index.schema + tile_index.table,
        'tile': tile_index.field.pk.sqlid
    }
    query = sql.SQL("""
    SELECT DISTINCT {tile}
    FROM {index_}
    WHERE {tile} IN {tiles}
    """).format(**query_params)
    log.debug(conn.print_query(query))
    in_index = [t[0] for t in conn.get_query(query)]
    not_found = set(tile_list) - set(in_index)
    if len(not_found) > 0:
        log.warning(f"The provided tile IDs {not_found} are not in the index, "
                    f"they are skipped.")
    return in_index

def all_in_index(conn: db.Db, tile_index: db.Schema) -> List[str]:
    """Get all tile IDs from the tile index."""
    query_params = {
        'index_': tile_index.schema + tile_index.table,
        'tile': tile_index.field.pk.sqlid
    }
    query = sql.SQL("""
    SELECT DISTINCT {tile} FROM {index_}
    """).format(**query_params)
    log.debug(conn.print_query(query))
    return [t[0] for t in conn.get_query(query)]

def parse_polygonz(wkt_polygonz):
    """Parses a POLYGON Z array of WKT into CityJSON Surface"""
    # match: 'POLYGON Z (<match everything in here>)'
    outer_pat = re.compile(r"(?<=POLYGON Z \().*(?!$)")
    # match: '(<match everything in here>)'
    ring_pat = re.compile(r"\(([^)]+)\)")
    outer = outer_pat.findall(wkt_polygonz)
    if len(outer) > 0:
        rings = ring_pat.findall(outer[0])
        for ring in rings:
            pts = [tuple(map(float, pt.split()))
                   for pt in ring.split(',')]
            yield pts[1:]  # WKT repeats the first vertex
    else:
        log.error("Not a POLYGON Z")


def query(conn_cfg: Mapping, tile_index: Mapping, cityobject_type: Mapping,
          threads=None,
          tile_list=None, bbox=None, extent=None):
    """Export a table from PostgreSQL. Multithreading, with connection pooling."""
    # see: https://realpython.com/intro-to-python-threading/
    # see: https://stackoverflow.com/a/39310039
    # Need one thread per table
    if threads is None:
        threads = sum(len(cotables) for cotables in cityobject_type.values())
    if threads == 1:
        log.debug(f"Running on a single thread.")
        conn = db.Db(**conn_cfg)
        try:
            for cotype, cotables in cityobject_type.items():
                for cotable in cotables:
                    tablename = cotable['table']
                    log.debug(f"CityObject {cotype} from table {cotable['table']}")
                    features = db.Schema(cotable)
                    tx = db.Schema(tile_index)
                    sql_query = build_query(conn=conn, features=features,
                                            tile_index=tx, tile_list=tile_list,
                                            bbox=bbox, extent=extent)
                    try:
                        # Note that resultset can be []
                        yield (cotype, cotable['table']), conn.get_dict(query)
                    except pgError as e:
                        log.error(f"{e.pgcode}\t{e.pgerror}")
                        raise ClickException(
                            f"Could not query {cotable}. Check the "
                            f"logs for details.")
        finally:
            conn.close()
    elif threads > 1:
        log.debug(f"Running with ThreadPoolExecutor, nr. of threads={threads}")
        pool_size = sum(len(cotables) for cotables in cityobject_type.values())
        conn_pool = pool.ThreadedConnectionPool(minconn=1,
                                                maxconn=pool_size+1,
                                                **conn_cfg)
        try:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                future_to_table = {}
                for cotype, cotables in cityobject_type.items():
                    # Need a thread for each of these
                    for cotable in cotables:
                        tablename = cotable['table']
                        # Need a connection from the pool per thread
                        conn = db.Db(conn=conn_pool.getconn(key=(cotype, tablename)))
                        # Need a connection and thread for each of these
                        log.debug(f"CityObject {cotype} from table {cotable['table']}")
                        features = db.Schema(cotable)
                        tx = db.Schema(tile_index)
                        sql_query = build_query(conn=conn, features=features,
                                                tile_index=tx, tile_list=tile_list,
                                                bbox=bbox, extent=extent)
                        # Schedule the DB query for execution and store the returned
                        # Future together with the cotype and table name
                        future = executor.submit(conn.get_dict, sql_query)
                        future_to_table[future] = (cotype, tablename)
                        # If I put away the connection here, then it locks the main
                        # thread and it becomes like using a single connection.
                        # conn_pool.putconn(conn=conn.conn, key=(cotype, tablename),
                        #                   close=True)
                for future in as_completed(future_to_table):
                    cotype, tablename = future_to_table[future]
                    try:
                        # Note that resultset can be []
                        yield (cotype, cotable['table']), future.result()
                    except pgError as e:
                        log.error(f"{e.pgcode}\t{e.pgerror}")
                        raise ClickException(f"Could not query {cotable}. Check the "
                                             f"logs for details.")
        finally:
            conn_pool.closeall()
    else:
        raise ValueError(f"Number of threads must be greater than 0.")

### start Multithreading optimisation tests ---

def _query_no_pool(conn_cfg: Mapping, tile_index: db.Schema, cityobject_type: Mapping,
                   threads=None,
                   tile_list=None, bbox=None, extent=None):
    """Export a table from PostgreSQL. Multithreading, without connection pooling."""
    # see: https://realpython.com/intro-to-python-threading/
    # see: https://stackoverflow.com/a/39310039
    # Need one thread per table
    if threads is None:
        threads = sum(len(cotables) for cotables in cityobject_type.values())
    log.debug(f"Number of threads={threads}")
    conn = db.Db(**conn_cfg)
    try:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            future_to_table = {}
            for cotype, cotables in cityobject_type.items():
                # Need a thread for each of these
                for cotable in cotables:
                    tablename = cotable['table']
                    # Need a connection from the pool per thread
                    # conn = db.Db(conn=conn_pool.getconn(key=(cotype, tablename)))
                    # Need a connection and thread for each of these
                    log.debug(f"CityObject {cotype} from table {cotable['table']}")
                    features = db.Schema(cotable)
                    query = build_query(conn=conn, features=features,
                                        tile_index=tile_index, tile_list=tile_list,
                                        bbox=bbox, extent=extent)
                    # Schedule the DB query for execution and store the returned
                    # Future together with the cotype and table name
                    future = executor.submit(conn.get_dict, query)
                    future_to_table[future] = (cotype, tablename)
                    # conn_pool.putconn(conn=conn.conn, key=(cotype, tablename),
                    #                   close=False)
            for future in as_completed(future_to_table):
                cotype, tablename = future_to_table[future]
                try:
                    # Note that resultset can be []
                    yield (cotype, cotable['table']), future.result()
                except pgError as e:
                    log.error(f"{e.pgcode}\t{e.pgerror}")
                    raise ClickException(f"Could not query {cotable}. Check the "
                                         f"logs for details.")
    finally:
        conn.close()

def _query_single(conn: db.Db, cfg: Mapping, tile_list=None, bbox=None,
                  extent=None):
    """Export a table from PostgreSQL. Single thread, single connection."""
    # Need a thread per tile
    tile_index = db.Schema(cfg['tile_index'])
    for cotype, cotables in cfg['cityobject_type'].items():
        # Need a thread for each of these
        for cotable in cotables:
            # Need a connection and thread for each of these
            log.info(f"CityObject {cotype} from table {cotable['table']}")
            features = db.Schema(cotable)
            query = build_query(conn=conn, features=features,
                                tile_index=tile_index, tile_list=tile_list,
                                bbox=bbox, extent=extent)
            try:
                tabledata =  conn.get_dict(query)
            except pgError as e:
                log.error(f"{e.pgcode}\t{e.pgerror}")
                raise ClickException(f"Could not query {cotable}. Check the "
                                     f"logs for details.")
            # Note that resultset can be []
            yield (cotype,cotable['table']), tabledata

### end Multithreading optimisation test ---

def table_to_cityobjects(tabledata, cotype: str, geomtype: str, lod: str):
    """Converts a database record to a CityObject."""
    for record in tabledata:
        coid = record['coid']
        co = CityObject(id=coid)
        # Parse the geometry
        # TODO: refactor geometry parsing into a function
        geom = Geometry(type=geomtype, lod=lod)
        if geomtype == 'Solid':
            solid = [record['geom'],]
            geom.boundaries = solid
        elif geomtype == 'MultiSurface':
            geom.boundaries = record['geom']
        co.geometry.append(geom)
        # Parse attributes
        for key, attr in record.items():
            if key != 'pk' and key != 'geom' and key != 'coid':
                if isinstance(attr, datetime):
                    co.attributes[key] = attr.isoformat()
                else:
                    co.attributes[key] = attr
        # Set the CityObject type
        co.type = cotype
        yield coid, co


def dbexport_to_cityobjects(dbexport, lod):
    for coinfo, tabledata in dbexport:
        cotype, cotable = coinfo
        if cotype.lower() == 'building':
            geomtype = 'Solid'
        else:
            # FIXME: because CompositeSurface is not supported at the moment
            #  for semantic surfaces in cjio.models
            geomtype = 'MultiSurface'

        # Loop through the whole tabledata and create the CityObjects
        cityobject_generator = table_to_cityobjects(
            tabledata=tabledata, cotype=cotype, geomtype=geomtype, lod=lod
        )
        for coid, co in cityobject_generator:
            yield coid, co


def convert(dbexport, lod: str):
    """Convert the exported citymodel to CityJSON."""
    # Set EPSG
    epsg = 7415
    cm = cityjson.CityJSON()
    cm.cityobjects = dict(dbexport_to_cityobjects(dbexport, lod=lod))
    log.debug("Referencing geometry")
    cityobjects, vertex_lookup = cm.reference_geometry()
    log.debug("Adding to json")
    cm.add_to_j(cityobjects, vertex_lookup)
    log.debug("Updating bbox")
    cm.update_bbox()
    log.debug("Setting EPSG")
    cm.set_epsg(epsg)
    log.info(f"Exported CityModel:\n{cm}")
    return cm


def _to_citymodel(filepath, dbexport) -> Tuple:
    try:
        cm = convert(dbexport)
    except BaseException as e:
        log.error(f"Failed to convert export to CityJSON\n{e}")
        return filepath, None
    if cm:
        try:
            cm.remove_duplicate_vertices()
        except BaseException as e:
            log.error(f"Failed to remove duplicate vertices\n{e}")
            return filepath, None
        try:
            cm.remove_orphan_vertices()
        except BaseException as e:
            log.error(f"Failed to remove orphan vertices\n{e}")
            return None, None
        return filepath, cm

def to_citymodel(dbexport, lod: str):
    try:
        cm = convert(dbexport, lod=lod)
    except BaseException as e:
        log.error(f"Failed to convert database export to CityJSON\n{e}")
        return None
    if cm:
        try:
            cm.remove_duplicate_vertices()
        except BaseException as e:
            log.error(f"Failed to remove duplicate vertices\n{e}")
            return None
        try:
            cm.remove_orphan_vertices()
        except BaseException as e:
            log.error(f"Failed to remove orphan vertices\n{e}")
            return None
        return cm

def export(tile, filepath, cfg):
    """Export a tile from PostgreSQL, convert to CityJSON and write to file."""
    try:
        dbexport = query(conn_cfg=cfg['database'], tile_index=cfg['tile_index'],
                         cityobject_type=cfg['cityobject_type'], threads=1,
                         tile_list=(tile,))
    except BaseException as e:
        log.error(f"Failed to export tile {str(tile)}\n{e}")
        return False, filepath
    try:
        cm = to_citymodel(dbexport, lod=cfg['lod'])
    finally:
        del dbexport
    if cm is not None:
        try:
            with open(filepath, 'w') as fout:
                json_str = json.dumps(cm.j, indent=None)
                fout.write(json_str)
            return True, filepath
        except IOError as e:
            log.error(f"Invalid output file: {filepath}\n{e}")
            return False, filepath
        finally:
            del cm
    else:
        log.error(f"Failed to create CityJSON from {filepath.stem},"
                   f" check the logs for details.")
        return False, filepath