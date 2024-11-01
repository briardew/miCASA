'''
MODIS/VIIRS vegetation index module
'''

# * Generalize to include VIIRS (will need to look for *.h5)
# * LAI?

from os import path
from glob import glob
from datetime import datetime

import numpy as np
import xarray as xr
import rioxarray as rxr

from modvir.config import defaults, NTYPE
from modvir.geometry import edges, centers, singrid

# Simple way to exclude most water, etc.
NDVIMIN = -0.3

# Read from yaml? Will want to estimate
# Has to match land cover type definitions
# Define as a dictionary?
NDVI02P = [0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330,
    0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330, 0.0330,
    0.0330]
NDVI98P = [0.7200, 0.8400, 0.8800, 0.8000, 0.8800, 0.8600, 0.7400, 0.7200,
    0.8000, 0.8000, 0.7200, 0.7800, 0.7800, 0.7800, 0.8200, 0.7200, 0.7200,
    0.7200]

# Los et al. (2000): https://doi.org/10.1175/1525-7541(2000)001<0183:AGYBLS>2.0.CO;2
fPMIN = 0.01
fPMAX = 0.95

def _regrid(dsout, dirin, mask=None):
    nlat = dsout.sizes['lat']
    nlon = dsout.sizes['lon']

    late, lone = edges(nlat, nlon)

    flist = glob(path.join(dirin, '*.hdf'))
    if len(flist) == 0:
        raise EOFError('no files to open')

    # Keeping num in case we want Red and NIR outputs
    num = np.zeros((nlat, nlon))
    red = np.zeros((nlat, nlon))
    nir = np.zeros((nlat, nlon))

    # Read and regrid (bin)
    # ---------------------
    for ff in flist:
        dsin = rxr.open_rasterio(ff).squeeze(drop=True)

        # Compute lat/lon mesh for MODIS sin grid
        LAin, LOin = singrid(dsin['y'].values, dsin['x'].values)

        # Read Red, NIR and QCs
        redin = dsin['Nadir_Reflectance_Band1'].values.T
        redqc = dsin['BRDF_Albedo_Band_Mandatory_Quality_Band1'].values.T
        nirin = dsin['Nadir_Reflectance_Band2'].values.T
        nirqc = dsin['BRDF_Albedo_Band_Mandatory_Quality_Band2'].values.T

        # Red and NIR can have different QC
        # QC = 255 and val = 32767 are equiv, but sometimes val = -32767
        # QC = 0 is too strict over cloudy regions, e.g., Amazon
        # QC = 1 is an over-agressive fill we must live with
        iok = np.logical_and.reduce((abs(redin) != 32767, redqc != 255,
            abs(nirin) != 32767, nirqc != 255,
            NDVIMIN*(nirin + redin) <= nirin - redin))

        numgran = np.histogram2d(LAin[iok], LOin[iok], bins=(late,lone))[0]
        redgran = np.histogram2d(LAin[iok], LOin[iok], bins=(late,lone),
            weights=redin[iok])[0]
        nirgran = np.histogram2d(LAin[iok], LOin[iok], bins=(late,lone),
            weights=nirin[iok])[0]

        num = num + numgran
        red = red + redgran
        nir = nir + nirgran
    dsin.close()

    # Divide without complaining about NaNs
    with np.errstate(divide='ignore', invalid='ignore'):
        ndvi = (nir - red)/(nir + red)

    # Apply mask if provided
    if mask is not None:
    # Should this mask to NDVIMIN or 0? Looks like GIMMS masks to NDVIMIN
        ndvi = (ndvi - NDVIMIN)*mask + NDVIMIN
#       ndvi = ndvi * mask

    # Fill Dataset
    dsout['NDVI'].values = ndvi.astype(dsout['NDVI'].dtype)
    dsout.attrs['input_files'] = ', '.join([path.basename(ff) for ff in flist])

    return dsout

# Keeping this for now, but obviously not working
def _ndvi2fpar_los(ndvi, lctype):
    '''Convert NDVI to fPAR using Los et al. (2000) formulation'''

    def srfun(xx):
        return (1. + xx)/(1. - xx)

    fpar = np.zeros_like(ndvi)

    # Convert to using percent
    for nn in range(NTYPE):
        ime = lctype == nn

        N0 = NDVI02P[nn]
        N1 = NDVI98P[nn]
        S0 = srfun(N0)
        S1 = srfun(N1)

        sr = srfun(np.minimum(ndvi,N1))

        fpsr = (sr   - S0)*(fPMAX - fPMIN)/(S1 - S0) + fPMIN
        fpnd = (ndvi - N0)*(fPMAX - fPMIN)/(N1 - N0) + fPMIN

        fpsr = np.maximum(fPMIN, np.minimum(fPMAX, fpsr))
        fpnd = np.maximum(fPMIN, np.minimum(fPMAX, fpnd))

        fpar[ime] = 0.5*(fpsr[ime] + fpnd[ime])

    fpar[np.isnan(fpar)] = 0.

    return fpar

def _ndvi2fpar_lin(ndvi):
    '''Convert NDVI to fPAR using simple linear transform'''

    fpar = np.zeros_like(ndvi)

    N0 = 0.10
    N1 = 0.80

    fpar = (ndvi - N0)*(fPMAX - fPMIN)/(N1 - N0) + fPMIN
    fpar = np.maximum(fPMIN, np.minimum(fPMAX, fpar))

    fpar[np.isnan(fpar)] = 0.

    return fpar

def _ndvi2fpar_jojo(ndvi):
    '''Convert NDVI to fPAR using Joiner et al. (2018) formulation'''

    fpar = np.zeros_like(ndvi)

#   N0 = 0.25
    N0 = 0.15
    N1 = 0.75

    iramp = np.logical_and(N0 < ndvi, ndvi <= N1)
    ifree = N1 < ndvi

    fpar[iramp] = (ndvi[iramp] - N0)/(N1 - N0)*N1
    fpar[ifree] =  ndvi[ifree]

    fpar[np.isnan(fpar)] = 0.

    return fpar

class VegInd(xr.Dataset):
    '''Vegetation index (NDVI/fPAR) class'''
    __slots__ = ()

    def __init__(self, dataset=None, nlat=defaults['nlat'],
        nlon=defaults['nlon']):
        if dataset is not None:
           self = xr.Dataset.__init__(self, dataset)
           return

        lat, lon = centers(nlat, nlon)

        coords = {'lat':(['lat'], lat.astype(np.single),
                {'long_name':'latitude','units':'degrees_north'}),
            'lon':(['lon'], lon.astype(np.single),
                {'long_name':'longitude','units':'degrees_east'})}

        blank = np.nan * np.ones((nlat, nlon))

        dandvi = xr.DataArray(data=blank.astype(np.single),
            dims=['lat','lon'], coords=coords,
            attrs={'long_name':'Normalized difference vegetation index (NDVI)',
                'units':'1'})

        self = xr.Dataset.__init__(self,
            data_vars={'NDVI':dandvi},
            # Read institution and contact from settings (***FIXME***)
            attrs={'Conventions':'CF-1.9',
                'institution':'NASA Goddard Space Flight Center',
                'contact':'Brad Weir <brad.weir@nasa.gov>',
                'title':'MODIS/VIIRS daily vegetation (NDVI/fPAR) data',
                'input_files':''})

    def ndvi2fpar(self, lctype):
#       fpar = _ndvi2fpar_los(self['NDVI'].values, lctype)
#       fpar = _ndvi2fpar_lin(self['NDVI'].values)
        fpar = _ndvi2fpar_jojo(self['NDVI'].values)

        return self.assign(fPAR=(['lat','lon'], fpar, {
            'long_name':'Fraction (absorbed) Photosynthetically Available ' +
            'Radiation (fPAR)', 'units':'1'}))

    def regrid(self, *args, **kwargs):
        return _regrid(self, *args, **kwargs)

    def to_netcdf(self, *args, **kwargs):
        # Fill history with (close enough) timestamp
        self.attrs['history'] = 'Created on ' + datetime.now().isoformat()

        # Set _FillValue to None instead of NaN by default
        if 'encoding' not in kwargs:
            kwargs['encoding'] = {var:{'_FillValue':None}
                for var in self.variables}

        return super().to_netcdf(*args, **kwargs)
