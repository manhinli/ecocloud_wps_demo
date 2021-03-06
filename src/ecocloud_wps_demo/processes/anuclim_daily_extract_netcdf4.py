import csv
import datetime
from itertools import takewhile, repeat
import logging
import operator
import os

from netCDF4 import Dataset
import numpy as np

from pywps import Process
from pywps import ComplexInput, ComplexOutput, LiteralInput, Format
from pywps.validator.mode import MODE

#from netCDF4 import Dataset

data = {
    'rainfall': {
        'url': 'http://dapds00.nci.org.au/thredds/dodsC/rr9/eMAST_data/ANUClimate/ANUClimate_v1-0_rainfall_daily_0-01deg_1970-2014',
        'variable': 'lwe_thickness_of_precipitation_amount',
    },
    'temp_max': {
        'url': 'http://dapds00.nci.org.au/thredds/dodsC/rr9/eMAST_data/ANUClimate/ANUClimate_v1-1_temperature-max_daily_0-01deg_1970-2014',
        'variable': 'air_temperature',
    },
    'temp_min': {
        'url': 'http://dapds00.nci.org.au/thredds/dodsC/rr9/eMAST_data/ANUClimate/ANUClimate_v1-1_temperature-min_daily_0-01deg_1970-2014',
        'variable': 'air_temperature',
    },
    'vapour_pressure_mean': {
        'url': 'http://dapds00.nci.org.au/thredds/dodsC/rr9/eMAST_data/ANUClimate/ANUClimate_v1-1_vapour-pressure_daily_0-01deg_1970-2014',
        'variable': 'vapour_pressure',
    },
    'solar_radiation_mean': {
        'url': 'http://dapds00.nci.org.au/thredds/dodsC/rr9/eMAST_data/ANUClimate/ANUClimate_v1-1_solar-radiation_daily_0-01deg_1970-2014',
        'variable': 'solar_radiation',
    }
}


# Process instances need to be picklable, because they are deep copied for execution.
# Ideally we don't need deepcopy, but for now this is what pywps does.
# if necessary see https://docs.python.org/3/library/pickle.html#handling-stateful-objects
# to make a class picklable or see copyreg module.
class ANUClimDailyExtractNetCDF4(Process):
    def __init__(self):
        inputs = [
            LiteralInput(
                'variables', 'Variables to extract',
                data_type='string', min_occurs=1, max_occurs=len(data),
                mode=MODE.SIMPLE, allowed_values=list(data.keys()),
            ),
            ComplexInput(
                'csv', 'CSV occurrences with date',
                supported_formats=[Format('text/csv')],
                min_occurs=1, max_occurs=1,
                # There is no CSV validator, so we have to use None
                mode=MODE.NONE
            ),
        ]

        outputs = [
            ComplexOutput('output', 'Metadata',
                          as_reference=True,
                          supported_formats=[Format('text/csv')]),
        ]

        super().__init__(
            self._handler,
            identifier='anuclim_daily_extract_netcdf4',
            title='ANUClim daily climate data extract.',
            abstract="Extracts env variables at specific location and time from ANUClimate daily climate grids.",
            version='1',
            metadata=[],
            inputs=inputs,
            outputs=outputs,
            store_supported=True,
            # TODO: birdy does not handle this? .. or rather if async call,
            #       birdy asks for status, but pywps process says no to it and fails the request
            status_supported=True)

    def _count_lines(self, filename):
        f = open(filename, 'rb')
        bufgen = takewhile(operator.truth,
                           (f.raw.read(1024 * 1024) for _ in repeat(None)))
        return sum(buf.count(b'\n') for buf in bufgen if buf)

    def _handler(self, request, response):
        # import pdb; pdb.set_trace()
        # Get the NetCDF file
        log = logging.getLogger(__name__)
        variables = [v.data for v in request.inputs['variables']]  # Note that all input parameters require index access
        dataset_csv = request.inputs['csv'][0]

        lines = self._count_lines(dataset_csv.file) - 1  # don't count header
        count = 0
        next_update = 0

        csv_fp = open(dataset_csv.file, 'r')
        csv_reader = csv.reader(csv_fp)
        csv_header = next(csv_reader)
        csv_header_idx = {col: csv_header.index(col) for col in csv_header}
        if any(col not in csv_header for col in ('lat', 'lon', 'date')):
            raise Exception('Bad trait data: Missing lat/lon/date column')

        # open opendap dataset
        datasets = {}
        for var in variables:
            # open dataset
            ds = Dataset(data[var]['url'])
            # fetch index arrays and get min/max
            lats = ds['lat']
            lons = ds['lon']
            times = ds['time']
            # becafse of using slice and tuple extraction we don't need .item
            lat_min, lat_max = lats[[0, -1]]
            if lat_min > lat_max:
                lat_min, lat_max = lat_max, lat_min
            lon_min, lon_max = lons[[0, -1]]
            if lon_min > lon_max:
                lon_min, lon_max = lon_max, lon_min
            time_min, time_max = times[[0, -1]]
            # get grid data
            grid = ds[data[var]['variable']]
            # store data
            datasets[var] = {
                'ds': ds,
                'data': grid,
                # get the actual index data
                'lats': lats[:],
                'lons': lons[:],
                'times': times[:],
                'lon_min': lon_min, 'lon_max': lon_max,
                'lat_min': lat_min, 'lat_max': lat_max,
                'time_min': time_min, 'time_max': time_max,
            }
        lats, lons, times = lats[:], lons[:], times[:]

        # append variables to csv header
        for var in variables:
            csv_header.append(var)

        # produce output file
        # TODO: may want to use resoponse.outputs['output'].workdir here
        out_csv = os.path.join(self.workdir, 'out.csv')
        with open(out_csv, 'w') as fp:
            # start processing
            csv_writer = csv.writer(fp)
            csv_writer.writerow(csv_header)
            # iterate through input csv
            for row in csv_reader:
                count += 1  # incr. line read rounter
                try:
                    # get coordinates from csv
                    edate = datetime.datetime.strptime(row[csv_header_idx['date']].strip(), '%Y-%m-%d').timestamp()
                    lat = float(row[csv_header_idx['lat']])
                    lon = float(row[csv_header_idx['lon']])
                    # TODO: should allow little buffer with cellsize to filter lat/lon
                    # FIXME: this check lat/lon min/max against last processed dataset
                    #        not a problem right now, because all variables have the same shape, but not ideal
                    if (lat_min - lat > 0) or (lat - lat_max > 0):
                        raise Exception('lat outside data area')
                    if (lon_min - lon > 0) or (lon - lon_max > 0):
                        raise Exception('lon outside data area')
                    if edate < time_min or edate > time_max:
                        raise Exception('time outside data area')
                    # find data indices
                    # FIXME: this get's data indices from last processed dataset
                    #        not a problem right now, because all variables have the same shape, but not ideal
                    edate_idx = np.abs(times - edate).argmin()
                    lat_idx = np.abs(lats - lat).argmin()
                    lon_idx = np.abs(lons - lon).argmin()
                    # extract data
                    for var in variables:
                        value = datasets[var]['data'][edate_idx, lat_idx, lon_idx]
                        row.append(value.item())
                except Exception as e:
                    # FIXME: this may also be reached due to an I/O Error ....
                    #        e.g. 'NetCDF: I/O failure' ... what type of exception would that be?
                    log.warn('Filling Row with empty values: {}'.format(e))
                    for var in variables:
                        row.append('')
                csv_writer.writerow(row)
                # done processing current line ...
                # update progress status
                percent = int(count / lines * 100)
                # don't re-generate status doc after every single line
                if (percent >= next_update):
                    next_update += 5
                    response.update_status(
                        'Processed lines {} of {} so far...'.format(count, lines),
                        percent
                    )
        # set output file
        # NOTE: assigning to output.file needs to be done after output file
        #       is finished. Assignment here copies the file to the outputs
        #       folder.
        response.outputs['output'].file = out_csv
        return response
