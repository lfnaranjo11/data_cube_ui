from django.db.models import F

from celery.task import task
from celery import chain, group, chord
from celery.utils.log import get_task_logger
from datetime import datetime, timedelta
import xarray as xr
import os
import imageio

from utils.data_cube_utilities.data_access_api import DataAccessApi
from utils.data_cube_utilities.dc_utilities import (
    create_cfmask_clean_mask, create_bit_mask, write_geotiff_from_xr, write_png_from_xr, write_single_band_png_from_xr,
    add_timestamp_data_to_xr, clear_attrs, perform_timeseries_analysis, nan_to_num)
from utils.data_cube_utilities.dc_chunker import (create_geographic_chunks, create_time_chunks,
                                                  combine_geographic_chunks)
from utils.data_cube_utilities.dc_water_quality import tsm, mask_water_quality
from apps.dc_algorithm.utils import create_2d_plot

from .models import TsmTask
from apps.dc_algorithm.models import Satellite
from apps.dc_algorithm.tasks import DCAlgorithmBase, check_cancel_task, task_clean_up

logger = get_task_logger(__name__)


class BaseTask(DCAlgorithmBase):
    app_name = 'tsm'


@task(name="tsm.pixel_drill", base=BaseTask)
def pixel_drill(task_id=None):
    parameters = parse_parameters_from_task(task_id=task_id)
    validate_parameters(parameters, task_id=task_id)
    task = TsmTask.objects.get(pk=task_id)

    if task.status == "ERROR":
        return None

    dc = DataAccessApi(config=task.config_path)
    single_pixel = dc.get_stacked_datasets_by_extent(**parameters)
    clear_mask = task.satellite.get_clean_mask_func()(single_pixel.isel(latitude=0, longitude=0))
    single_pixel = single_pixel.where(single_pixel != task.satellite.no_data_value)

    dates = single_pixel.time.values
    if len(dates) < 2:
        task.update_status("ERROR", "There is only a single acquisition for your parameter set.")
        return None

    wofs_data = task.get_processing_method()(single_pixel,
                                             clean_mask=clear_mask,
                                             enforce_float64=True,
                                             no_data=task.satellite.no_data_value)
    wofs_data = wofs_data.where(wofs_data != task.satellite.no_data_value).isel(latitude=0, longitude=0)
    tsm_data = tsm(single_pixel, clean_mask=clear_mask, no_data=task.satellite.no_data_value)
    tsm_data = tsm_data.where(tsm_data != task.satellite.no_data_value).isel(
        latitude=0, longitude=0).where((wofs_data.wofs.values == 1))

    datasets = [wofs_data.wofs.values.transpose(), tsm_data.tsm.values.transpose()] + [clear_mask]
    data_labels = ["Water/Non Water", "TSM (g/L)"] + ["Clear"]
    titles = ["Water/Non Water", "TSM Values"] + ["Clear Mask"]
    style = ['.', 'r-o', '.']

    task.plot_path = os.path.join(task.get_result_path(), "plot_path.png")
    create_2d_plot(task.plot_path, dates=dates, datasets=datasets, data_labels=data_labels, titles=titles, style=style)

    task.complete = True
    task.update_status("OK", "Done processing pixel drill.")


@task(name="tsm.run", base=BaseTask)
def run(task_id=None):
    """Responsible for launching task processing using celery asynchronous processes

    Chains the parsing of parameters, validation, chunking, and the start to data processing.
    """
    return chain(parse_parameters_from_task.s(task_id=task_id),
                 validate_parameters.s(task_id=task_id),
                 perform_task_chunking.s(task_id=task_id),
                 start_chunk_processing.s(task_id=task_id))()


@task(name="tsm.parse_parameters_from_task", base=BaseTask, bind=True)
def parse_parameters_from_task(self, task_id=None):
    """Parse out required DC parameters from the task model.

    See the DataAccessApi docstrings for more information.
    Parses out platforms, products, etc. to be used with DataAccessApi calls.

    If this is a multisensor app, platform and product should be pluralized and used
    with the get_stacked_datasets_by_extent call rather than the normal get.

    Returns:
        parameter dict with all keyword args required to load data.
    """
    task = TsmTask.objects.get(pk=task_id)

    parameters = {
        'platforms': task.satellite.get_platforms(),
        'products': task.satellite.get_products(task.area_id),
        'time': (task.time_start, task.time_end),
        'longitude': (task.longitude_min, task.longitude_max),
        'latitude': (task.latitude_min, task.latitude_max),
        'measurements': task.satellite.get_measurements()
    }

    task.execution_start = datetime.now()
    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Parsed out parameters.")

    return parameters


@task(name="tsm.validate_parameters", base=BaseTask, bind=True)
def validate_parameters(self, parameters, task_id=None):
    """Validate parameters generated by the parameter parsing task

    All validation should be done here - are there data restrictions?
    Combinations that aren't allowed? etc.

    Returns:
        parameter dict with all keyword args required to load data.
        -or-
        updates the task with ERROR and a message, returning None
    """
    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    dc = DataAccessApi(config=task.config_path)

    acquisitions = dc.list_combined_acquisition_dates(**parameters)

    if len(acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquistions for this parameter set.")
        return None

    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Validated parameters.")

    if not dc.validate_measurements(parameters['products'][0], parameters['measurements']):
        task.complete = True
        task.update_status(
            "ERROR",
            "The provided Satellite model measurements aren't valid for the product. Please check the measurements listed in the {} model.".
            format(task.satellite.name))
        return None

    dc.close()
    return parameters


@task(name="tsm.perform_task_chunking", base=BaseTask, bind=True)
def perform_task_chunking(self, parameters, task_id=None):
    """Chunk parameter sets into more manageable sizes

    Uses functions provided by the task model to create a group of
    parameter sets that make up the arg.

    Args:
        parameters: parameter stream containing all kwargs to load data

    Returns:
        parameters with a list of geographic and time ranges
    """
    if parameters is None:
        return None

    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    dc = DataAccessApi(config=task.config_path)
    dates = dc.list_combined_acquisition_dates(**parameters)
    task_chunk_sizing = task.get_chunk_size()

    geographic_chunks = create_geographic_chunks(
        longitude=parameters['longitude'],
        latitude=parameters['latitude'],
        geographic_chunk_size=task_chunk_sizing['geographic'])

    time_chunks = create_time_chunks(
        dates, _reversed=task.get_reverse_time(), time_chunk_size=task_chunk_sizing['time'])
    logger.info("Time chunks: {}, Geo chunks: {}".format(len(time_chunks), len(geographic_chunks)))

    dc.close()
    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Chunked parameter set.")
    return {'parameters': parameters, 'geographic_chunks': geographic_chunks, 'time_chunks': time_chunks}


@task(name="tsm.start_chunk_processing", base=BaseTask, bind=True)
def start_chunk_processing(self, chunk_details, task_id=None):
    """Create a fully asyncrhonous processing pipeline from paramters and a list of chunks.

    The most efficient way to do this is to create a group of time chunks for each geographic chunk,
    recombine over the time index, then combine geographic last.
    If we create an animation, this needs to be reversed - e.g. group of geographic for each time,
    recombine over geographic, then recombine time last.

    The full processing pipeline is completed, then the create_output_products task is triggered, completing the task.
    """
    if chunk_details is None:
        return None

    parameters = chunk_details.get('parameters')
    geographic_chunks = chunk_details.get('geographic_chunks')
    time_chunks = chunk_details.get('time_chunks')

    task = TsmTask.objects.get(pk=task_id)

    # Get an estimate of the amount of work to be done: the number of scenes
    # to process, also considering intermediate chunks to be combined.
    num_scenes = len(geographic_chunks) * sum([len(time_chunk) for time_chunk in time_chunks])
    logger.info("num_scenes: {}".format(num_scenes))
    # recombine_geographic_chunks() scenes:
    # num_scn_per_chk * len(time_chunks) * len(geographic_chunks)
    num_scn_per_chk = round(num_scenes / (len(time_chunks) * len(geographic_chunks)))
    # Scene processing progress is tracked in processing_task() and recombine_geographic_chunks().
    task.total_scenes = 2 * num_scenes
    logger.info("task.total_scenes: {}"
                .format(task.total_scenes))
    task.scenes_processed = 0
    task.save(update_fields=['total_scenes', 'scenes_processed'])

    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Starting processing.")

    logger.info("START_CHUNK_PROCESSING")

    processing_pipeline = (group([
        group([
            processing_task.s(
                task_id=task_id,
                geo_chunk_id=geo_index,
                time_chunk_id=time_index,
                geographic_chunk=geographic_chunk,
                time_chunk=time_chunk,
                **parameters) for geo_index, geographic_chunk in enumerate(geographic_chunks)
        ]) | recombine_geographic_chunks.s(task_id=task_id, num_scn_per_chk=num_scn_per_chk)
        for time_index, time_chunk in enumerate(time_chunks)
    ]) | recombine_time_chunks.s(task_id=task_id) | create_output_products.s(task_id=task_id)\
       | task_clean_up.si(task_id=task_id, task_model='TsmTask')).apply_async()

    return True


@task(name="tsm.processing_task", acks_late=True, base=BaseTask, bind=True)
def processing_task(self,
                    task_id=None,
                    geo_chunk_id=None,
                    time_chunk_id=None,
                    geographic_chunk=None,
                    time_chunk=None,
                    **parameters):
    """Process a parameter set and save the results to disk.

    Uses the geographic and time chunk id to identify output products.
    **params is updated with time and geographic ranges then used to load data.
    the task model holds the iterative property that signifies whether the algorithm
    is iterative or if all data needs to be loaded at once.

    Args:
        task_id, geo_chunk_id, time_chunk_id: identification for the main task and what chunk this is processing
        geographic_chunk: range of latitude and longitude to load - dict with keys latitude, longitude
        time_chunk: list of acquisition dates
        parameters: all required kwargs to load data.

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    chunk_id = "_".join([str(geo_chunk_id), str(time_chunk_id)])
    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    logger.info("Starting chunk: " + chunk_id)
    if not os.path.exists(task.get_temp_path()):
        return None

    metadata = {}

    def _get_datetime_range_containing(*time_ranges):
        return (min(time_ranges) - timedelta(microseconds=1), max(time_ranges) + timedelta(microseconds=1))

    times = list(
        map(_get_datetime_range_containing, time_chunk)
        if task.get_iterative() else [_get_datetime_range_containing(time_chunk[0], time_chunk[-1])])
    dc = DataAccessApi(config=task.config_path)
    updated_params = parameters
    updated_params.update(geographic_chunk)
    #updated_params.update({'products': parameters['']})
    water_analysis = None
    tsm_analysis = None
    combined_data = None
    base_index = (task.get_chunk_size()['time'] if task.get_chunk_size()['time'] is not None else 1) * time_chunk_id
    for time_index, time in enumerate(times):
        updated_params.update({'time': time})
        data = dc.get_stacked_datasets_by_extent(**updated_params)

        if check_cancel_task(self, task): return

        if data is None or 'time' not in data:
            logger.info("Invalid chunk.")
            continue

        clear_mask = task.satellite.get_clean_mask_func()(data)

        wofs_data = task.get_processing_method()(data,
                                                 clean_mask=clear_mask,
                                                 enforce_float64=True,
                                                 no_data=task.satellite.no_data_value)
        water_analysis = perform_timeseries_analysis(
            wofs_data, 'wofs', intermediate_product=water_analysis, no_data=task.satellite.no_data_value)

        clear_mask[(data.swir2.values > 100) | (wofs_data.wofs.values == 0)] = False
        tsm_data = tsm(data, clean_mask=clear_mask, no_data=task.satellite.no_data_value)
        tsm_analysis = perform_timeseries_analysis(
            tsm_data, 'tsm', intermediate_product=tsm_analysis, no_data=task.satellite.no_data_value)

        if check_cancel_task(self, task): return

        combined_data = tsm_analysis
        combined_data['wofs'] = water_analysis.total_data
        combined_data['wofs_total_clean'] = water_analysis.total_clean

        metadata = task.metadata_from_dataset(metadata, tsm_data, clear_mask, updated_params)
        if task.animated_product.animation_id != "none":
            path = os.path.join(task.get_temp_path(),
                                "animation_{}_{}.nc".format(str(geo_chunk_id), str(base_index + time_index)))
            animated_data = tsm_data.isel(
                time=0, drop=True) if task.animated_product.animation_id == "scene" else combined_data
            animated_data.to_netcdf(path)

        task.scenes_processed = F('scenes_processed') + 1
        task.save(update_fields=['scenes_processed'])
    if combined_data is None:
        return None
    path = os.path.join(task.get_temp_path(), chunk_id + ".nc")
    combined_data.to_netcdf(path)
    dc.close()
    logger.info("Done with chunk: " + chunk_id)
    return path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="tsm.recombine_geographic_chunks", base=BaseTask, bind=True)
def recombine_geographic_chunks(self, chunks, task_id=None, num_scn_per_chk=None):
    """Recombine processed data over the geographic indices

    For each geographic chunk process spawned by the main task, open the resulting dataset
    and combine it into a single dataset. Combine metadata as well, writing to disk.

    Args:
        chunks: list of the return from the processing_task function - path, metadata, and {chunk ids}
        num_scn_per_chk: The number of scenes per chunk. Used to determine task progress.

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    total_chunks = [chunks] if not isinstance(chunks, list) else chunks
    total_chunks = [chunk for chunk in total_chunks if chunk is not None]
    if len(total_chunks) == 0:
        return None
    geo_chunk_id = total_chunks[0][2]['geo_chunk_id']
    time_chunk_id = total_chunks[0][2]['time_chunk_id']

    metadata = {}
    chunk_data = []
    for index, chunk in enumerate(total_chunks):
        metadata = task.combine_metadata(metadata, chunk[1])
        chunk_data.append(xr.open_dataset(chunk[0]))
        task.scenes_processed = F('scenes_processed') + num_scn_per_chk
        task.save(update_fields=['scenes_processed'])
    combined_data = combine_geographic_chunks(chunk_data)

    if task.animated_product.animation_id != "none":
        base_index = (task.get_chunk_size()['time'] if task.get_chunk_size()['time'] is not None else 1) * time_chunk_id
        for index in range((task.get_chunk_size()['time'] if task.get_chunk_size()['time'] is not None else 1)):
            animated_data = []
            for chunk in total_chunks:
                geo_chunk_index = chunk[2]['geo_chunk_id']
                # if we're animating, combine it all and save to disk.
                path = os.path.join(task.get_temp_path(),
                                    "animation_{}_{}.nc".format(str(geo_chunk_index), str(base_index + index)))
                if os.path.exists(path):
                    animated_data.append(xr.open_dataset(path))
            path = os.path.join(task.get_temp_path(), "animation_{}.nc".format(base_index + index))
            if len(animated_data) > 0:
                combine_geographic_chunks(animated_data).to_netcdf(path)

    path = os.path.join(task.get_temp_path(), "recombined_geo_{}.nc".format(time_chunk_id))
    combined_data.to_netcdf(path)
    logger.info("Done combining geographic chunks for time: " + str(time_chunk_id))
    return path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="tsm.recombine_time_chunks", base=BaseTask, bind=True)
def recombine_time_chunks(self, chunks, task_id=None):
    """Recombine processed chunks over the time index.

    Open time chunked processed datasets and recombine them using the same function
    that was used to process them. This assumes an iterative algorithm - if it is not, then it will
    simply return the data again.

    Args:
        chunks: list of the return from the processing_task function - path, metadata, and {chunk ids}

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    # sorting based on time id - earlier processed first as they're incremented e.g. 0, 1, 2..
    chunks = chunks if isinstance(chunks, list) else [chunks]
    chunks = [chunk for chunk in chunks if chunk is not None]
    if len(chunks) == 0:
        return None
    total_chunks = sorted(chunks, key=lambda x: x[0])
    geo_chunk_id = total_chunks[0][2]['geo_chunk_id']
    time_chunk_id = total_chunks[0][2]['time_chunk_id']
    metadata = {}

    def combine_intermediates(dataset, dataset_intermediate):
        """
        functions used to combine time sliced data after being combined geographically.
        This compounds the results of the time slice and recomputes the normalized data.
        """
        # total data/clean refers to tsm
        dataset_intermediate['total_data'] += dataset.total_data
        dataset_intermediate['total_clean'] += dataset.total_clean
        dataset_intermediate['normalized_data'] = dataset_intermediate['total_data'] / dataset_intermediate[
            'total_clean']
        dataset_intermediate['min'] = xr.concat(
            [dataset_intermediate['min'], dataset['min']], dim='time').min(
                dim='time', skipna=True)
        dataset_intermediate['max'] = xr.concat(
            [dataset_intermediate['max'], dataset['max']], dim='time').max(
                dim='time', skipna=True)
        dataset_intermediate['wofs'] += dataset.wofs
        dataset_intermediate['wofs_total_clean'] += dataset.wofs_total_clean

    def generate_animation(index, combined_data):
        base_index = (task.get_chunk_size()['time'] if task.get_chunk_size()['time'] is not None else 1) * index
        for index in range((task.get_chunk_size()['time'] if task.get_chunk_size()['time'] is not None else 1)):
            path = os.path.join(task.get_temp_path(), "animation_{}.nc".format(base_index + index))
            if os.path.exists(path):
                animated_data = xr.open_dataset(path)
                if task.animated_product.animation_id != "scene" and combined_data:
                    combine_intermediates(combined_data, animated_data)
                # need to wait until last step to mask out wofs < 0.8
                path = os.path.join(task.get_temp_path(), "animation_final_{}.nc".format(base_index + index))
                animated_data.to_netcdf(path)

    combined_data = None
    for index, chunk in enumerate(total_chunks):
        metadata.update(chunk[1])
        data = xr.open_dataset(chunk[0])
        if combined_data is None:
            if task.animated_product.animation_id != "none":
                generate_animation(index, combined_data)
            combined_data = data
            continue
        combine_intermediates(data, combined_data)
        if check_cancel_task(self, task): return
        # if we're animating, combine it all and save to disk.
        if task.animated_product.animation_id != "none":
            generate_animation(index, combined_data)

    path = os.path.join(task.get_temp_path(), "recombined_time_{}.nc".format(geo_chunk_id))
    combined_data.to_netcdf(path)
    logger.info("Done combining time chunks for geo: " + str(geo_chunk_id))
    return path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="tsm.create_output_products", base=BaseTask, bind=True)
def create_output_products(self, data, task_id=None):
    """Create the final output products for this algorithm.

    Open the final dataset and metadata and generate all remaining metadata.
    Convert and write the dataset to variuos formats and register all values in the task model
    Update status and exit.

    Args:
        data: tuple in the format of processing_task function - path, metadata, and {chunk ids}
    """
    task = TsmTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    full_metadata = data[1]
    dataset = xr.open_dataset(data[0]).astype('float64')
    dataset['variability'] = dataset['max'] - dataset['normalized_data']
    dataset['wofs'] = dataset.wofs / dataset.wofs_total_clean
    nan_to_num(dataset, 0)
    dataset_masked = mask_water_quality(dataset, dataset.wofs)

    task.result_path = os.path.join(task.get_result_path(), "tsm.png")
    task.clear_observations_path = os.path.join(task.get_result_path(), "clear_observations.png")
    task.water_percentage_path = os.path.join(task.get_result_path(), "water_percentage.png")
    task.data_path = os.path.join(task.get_result_path(), "data_tif.tif")
    task.data_netcdf_path = os.path.join(task.get_result_path(), "data_netcdf.nc")
    task.animation_path = os.path.join(task.get_result_path(),
                                       "animation.gif") if task.animated_product.animation_id != 'none' else ""
    task.final_metadata_from_dataset(dataset_masked)
    task.metadata_from_dict(full_metadata)

    bands = [task.query_type.data_variable, 'total_clean', 'wofs']
    band_paths = [task.result_path, task.clear_observations_path, task.water_percentage_path]

    dataset_masked.to_netcdf(task.data_netcdf_path)
    write_geotiff_from_xr(task.data_path, dataset_masked, bands=bands, no_data=task.satellite.no_data_value)

    for band, band_path in zip(bands, band_paths):
        write_single_band_png_from_xr(
            band_path,
            dataset_masked,
            band,
            color_scale=task.color_scales[band],
            fill_color='black',
            interpolate=False,
            no_data=task.satellite.no_data_value)

    if task.animated_product.animation_id != "none":
        with imageio.get_writer(task.animation_path, mode='I', duration=1.0) as writer:
            valid_range = range(len(full_metadata))
            for index in valid_range:
                path = os.path.join(task.get_temp_path(), "animation_final_{}.nc".format(index))
                if os.path.exists(path):
                    png_path = os.path.join(task.get_temp_path(), "animation_{}.png".format(index))
                    animated_data = mask_water_quality(
                        xr.open_dataset(path).astype('float64'),
                        dataset.wofs) if task.animated_product.animation_id != "scene" else xr.open_dataset(
                            path)
                    write_single_band_png_from_xr(
                        png_path,
                        animated_data,
                        task.animated_product.data_variable,
                        color_scale=task.color_scales[task.animated_product.data_variable],
                        fill_color='black',
                        interpolate=False,
                        no_data=task.satellite.no_data_value)
                    image = imageio.imread(png_path)
                    writer.append_data(image)

    dates = list(map(lambda x: datetime.strptime(x, "%m/%d/%Y"), task._get_field_as_list('acquisition_list')))
    if len(dates) > 1:
        task.plot_path = os.path.join(task.get_result_path(), "plot_path.png")
        create_2d_plot(
            task.plot_path,
            dates=dates,
            datasets=task._get_field_as_list('clean_pixel_percentages_per_acquisition'),
            data_labels="Clean Pixel Percentage (%)",
            titles="Clean Pixel Percentage Per Acquisition")

    logger.info("All products created.")
    task.update_bounds_from_dataset(dataset_masked)
    task.complete = True
    task.execution_end = datetime.now()
    task.update_status("OK", "All products have been generated. Your result will be loaded on the map.")
    return True