"""Functions for loading and saving cubes."""
import copy
import logging
import os
import shutil
from collections import OrderedDict
from itertools import groupby
from warnings import catch_warnings, filterwarnings

import iris
import iris.exceptions
import numpy as np
import yaml

from .._task import write_ncl_settings
from ._time import extract_time

logger = logging.getLogger(__name__)

GLOBAL_FILL_VALUE = 1e+20

DATASET_KEYS = {
    'mip',
}
VARIABLE_KEYS = {
    'reference_dataset',
    'alternative_dataset',
}


def _get_attr_from_field_coord(ncfield, coord_name, attr):
    if coord_name is not None:
        attrs = ncfield.cf_group[coord_name].cf_attrs()
        attr_val = [value for (key, value) in attrs if key == attr]
        if attr_val:
            return attr_val[0]
    return None


def concatenate_callback(raw_cube, field, _):
    """Use this callback to fix anything Iris tries to break."""
    # Remove attributes that cause issues with merging and concatenation
    for attr in ['creation_date', 'tracking_id', 'history']:
        if attr in raw_cube.attributes:
            del raw_cube.attributes[attr]
    for coord in raw_cube.coords():
        # Iris chooses to change longitude and latitude units to degrees
        # regardless of value in file, so reinstating file value
        if coord.standard_name in ['longitude', 'latitude']:
            units = _get_attr_from_field_coord(field, coord.var_name, 'units')
            if units is not None:
                coord.units = units


def load(file, callback=None):
    """Load iris cubes from files."""
    logger.debug("Loading:\n%s", file)
    with catch_warnings():
        filterwarnings(
            'ignore',
            message="Missing CF-netCDF measure variable .*",
            category=UserWarning,
            module='iris',
        )
        raw_cubes = iris.load_raw(file, callback=callback)
    if not raw_cubes:
        raise Exception('Can not load cubes from {0}'.format(file))
    for cube in raw_cubes:
        cube.attributes['source_file'] = file
    return raw_cubes


def _fix_cube_attributes(cubes):
    """Unify attributes of different cubes to allow concatenation."""
    attributes = {}
    for cube in cubes:
        for (attr, val) in cube.attributes.items():
            if attr not in attributes:
                attributes[attr] = val
            else:
                if not np.array_equal(val, attributes[attr]):
                    attributes[attr] = '{};{}'.format(str(attributes[attr]),
                                                      str(val))
    for cube in cubes:
        cube.attributes = attributes


def concatenate(cubes):
    """Concatenate all cubes after fixing metadata."""
    _fix_cube_attributes(cubes)

    concatenated = iris.cube.CubeList(cubes).concatenate()
    if len(concatenated) == 2:
        try:
            concatenated[0].coord('time')
            concatenated[1].coord('time')
        except iris.exceptions.CoordinateNotFoundError:
            pass
        else:
            concatenated = _concatenate_overlapping_cubes(concatenated)

    if len(concatenated) == 1:
        return concatenated[0]

    logger.error('Can not concatenate cubes into a single one.')
    logger.error('Resulting cubes:')
    for cube in concatenated:
        logger.error(cube)
        try:
            time = cube.coord('time')
        except iris.exceptions.CoordinateNotFoundError:
            pass
        else:
            logger.error('From %s to %s', time.cell(0), time.cell(-1))
    raise ValueError('Can not concatenate cubes.')


def save(cubes, filename, optimize_access='', compress=False, **kwargs):
    """
    Save iris cubes to file.

    Parameters
    ----------
    cubes: iterable of iris.cube.Cube
        Data cubes to be saved

    filename: str
        Name of target file

    optimize_access: str
        Set internal NetCDF chunking to favour a reading scheme

        Values can be map or timeseries, which improve performance when
        reading the file one map or time series at a time.
        Users can also provide a coordinate or a list of coordinates. In that
        case the better performance will be avhieved by loading all the values
        in that coordinate at a time

    compress: bool, optional
        Use NetCDF internal compression.

    Returns
    -------
    str
        filename

    """
    # Rename some arguments
    kwargs['target'] = filename
    kwargs['zlib'] = compress

    dirname = os.path.dirname(filename)
    if not os.path.exists(dirname):
        os.makedirs(dirname)

    if (os.path.exists(filename)
            and all(cube.has_lazy_data() for cube in cubes)):
        logger.debug(
            "Not saving cubes %s to %s to avoid data loss. "
            "The cube is probably unchanged.", cubes, filename)
        return filename

    logger.debug("Saving cubes %s to %s", cubes, filename)
    if optimize_access:
        cube = cubes[0]
        if optimize_access == 'map':
            dims = set(
                cube.coord_dims('latitude') + cube.coord_dims('longitude'))
        elif optimize_access == 'timeseries':
            dims = set(cube.coord_dims('time'))
        else:
            dims = tuple()
            for coord_dims in (cube.coord_dims(dimension)
                               for dimension in optimize_access.split(' ')):
                dims += coord_dims
            dims = set(dims)

        kwargs['chunksizes'] = tuple(
            length if index in dims else 1
            for index, length in enumerate(cube.shape))

    kwargs['fill_value'] = GLOBAL_FILL_VALUE
    iris.save(cubes, **kwargs)

    return filename


def _get_debug_filename(filename, step):
    """Get a filename for debugging the preprocessor."""
    dirname = os.path.splitext(filename)[0]
    if os.path.exists(dirname) and os.listdir(dirname):
        num = int(sorted(os.listdir(dirname)).pop()[:2]) + 1
    else:
        num = 0
    filename = os.path.join(dirname, '{:02}_{}.nc'.format(num, step))
    return filename


def cleanup(files, remove=None):
    """Clean up after running the preprocessor."""
    if remove is None:
        remove = []

    for path in remove:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)

    return files


def _ordered_safe_dump(data, stream):
    """Write data containing OrderedDicts to yaml file."""
    class _OrderedDumper(yaml.SafeDumper):
        pass

    def _dict_representer(dumper, data):
        return dumper.represent_mapping(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, data.items())

    _OrderedDumper.add_representer(OrderedDict, _dict_representer)
    return yaml.dump(data, stream, _OrderedDumper)


def write_metadata(products, write_ncl=False):
    """Write product metadata to file."""
    output_files = []
    for output_dir, prods in groupby(products,
                                     lambda p: os.path.dirname(p.filename)):
        sorted_products = sorted(
            prods,
            key=lambda p: (
                p.attributes.get('recipe_dataset_index', 1e6),
                p.attributes.get('dataset', ''),
            ),
        )
        metadata = OrderedDict()
        for product in sorted_products:
            if isinstance(product.attributes.get('exp'), (list, tuple)):
                product.attributes = dict(product.attributes)
                product.attributes['exp'] = '-'.join(product.attributes['exp'])
            metadata[product.filename] = product.attributes

        output_filename = os.path.join(output_dir, 'metadata.yml')
        output_files.append(output_filename)
        with open(output_filename, 'w') as file:
            _ordered_safe_dump(metadata, file)
        if write_ncl:
            output_files.append(_write_ncl_metadata(output_dir, metadata))

    return output_files


def _write_ncl_metadata(output_dir, metadata):
    """Write NCL metadata files to output_dir."""
    variables = [copy.deepcopy(v) for v in metadata.values()]

    info = {'input_file_info': variables}

    # Split input_file_info into dataset and variable properties
    # dataset keys and keys with non-identical values will be stored
    # in dataset_info, the rest in variable_info
    variable_info = {}
    info['variable_info'] = [variable_info]
    info['dataset_info'] = []
    for variable in variables:
        dataset_info = {}
        info['dataset_info'].append(dataset_info)
        for key in variable:
            dataset_specific = any(variable[key] != var.get(key, object())
                                   for var in variables)
            if ((dataset_specific or key in DATASET_KEYS)
                    and key not in VARIABLE_KEYS):
                dataset_info[key] = variable[key]
            else:
                variable_info[key] = variable[key]

    filename = os.path.join(output_dir,
                            variable_info['short_name'] + '_info.ncl')
    write_ncl_settings(info, filename)

    return filename


def _concatenate_overlapping_cubes(cubes):
    """Concatenate time-overlapping cubes (two cubes only)."""
    # we arrange [cube1, cube2] so that cube1.start <= cube2.start
    if cubes[0].coord('time').points[0] <= cubes[1].coord('time').points[0]:
        cubes = [cubes[0], cubes[1]]
        logger.debug(f"Will attempt to concatenate cubes %s "
                     f"and %s in this order", cubes[0], cubes[1])
    else:
        cubes = [cubes[1], cubes[0]]
        logger.debug(f"Will attempt to concatenate cubes %s "
                     f"and %s in this order", cubes[1], cubes[0])

    # get time end points
    time_1 = cubes[0].coord('time')
    time_2 = cubes[1].coord('time')
    data_start_1 = time_1.cell(0).point
    data_start_2 = time_2.cell(0).point
    data_end_1 = time_1.cell(-1).point
    data_end_2 = time_2.cell(-1).point

    # case 1: both cubes start at the same time -> return longer cube
    if data_start_1 == data_start_2:
        if data_end_1 < data_end_2:
            logger.debug(f"Both cubes start at the same time but cube %s "
                         f"ends before %s", cubes[0], cubes[1])
            logger.debug(f"Cube %s contains all needed data "
                         f"so using it fully", cubes[1])
            cubes = [cubes[1]]
        else:
            logger.debug(f"Both cubes start at the same time but cube %s "
                         f"ends before %s", cubes[1], cubes[0])
            logger.debug(f"Cube %s contains all needed data "
                         f"so using it fully", cubes[0])
            cubes = [cubes[0]]

    # case 2: cube1 starts before cube2
    else:
        # find time overlap, if any
        start_overlap = next(
            (time_1.units.num2date(t) for t in time_1.points
             if t in time_2.points),
            None
        )
        # case 0: no overlap (new iris implementaion does allow
        # concatenation of cubes with no overlap)
        if not start_overlap:
            raise ValueError(f"Attempting to concatenate cubes that are"
                             f"separated in time.")
        # case 2.1: cube1 ends before cube2 -> use full cube2 and shorten cube1
        if start_overlap and data_end_1 <= data_end_2:
            logger.debug(f"Extracting time slice between %s and %s "
                         f"from cube %s to use it for concatenation "
                         f"with cube %s",
                         "-".join([str(data_start_1.year),
                                   str(data_start_1.month),
                                   str(data_start_1.day)]),
                         "-".join([str(start_overlap.year),
                                   str(start_overlap.month),
                                   str(start_overlap.day)]),
                         cubes[0], cubes[1])
            c1_delta = extract_time(
                cubes[0],
                data_start_1.year, data_start_1.month, data_start_1.day,
                start_overlap.year, start_overlap.month, start_overlap.day
            )
            cubes = iris.cube.CubeList([c1_delta, cubes[1]])
            logger.debug("Attemptong concatenatenation of %s with %s",
                         c1_delta, cubes[1])
            try:
                cubes = [iris.cube.CubeList(cubes).concatenate_cube()]
            except iris.exceptions.ConcatenateError as ex:
                logger.error('Can not concatenate cubes: %s', ex)
                logger.error('Cubes:')
                for cube in cubes:
                    logger.error(cube)
                raise ex
        # case 2.2: cube1 ends after cube2 -> return cube1
        if start_overlap and data_end_1 > data_end_2:
            cubes = [cubes[0]]
            logger.debug("Using only data from %s", cubes[0])
        # case 2.3: there is no overlap: return original cubes

    return cubes
