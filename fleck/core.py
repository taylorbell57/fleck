import numpy as np
import astropy.units as u
from astropy.coordinates import (CartesianRepresentation,
                                 UnitSphericalRepresentation)
from astropy.coordinates.matrix_utilities import rotation_matrix
from scipy.integrate import quad
from shapely.geometry.point import Point
from shapely import affinity
from batman import TransitModel
import matplotlib.pyplot as plt

__all__ = ['Star', 'generate_spots']


def limb_darkening(u_ld, r):
    """
    Quadratic limb darkening function.

    Parameters
    ----------
    u_ld : list
        Quadratic limb-darkening parameters
    r : float or `~numpy.ndarray`
        Radius in units of stellar radii.

    Returns
    -------
    f : float or `~numpy.ndarray`
        Flux at ``r``.
    """
    u1, u2 = u_ld
    mu = np.sqrt(1 - r**2)
    return (1 - u1 * (1 - mu) - u2 * (1 - mu)**2) / (1 - u1/3 - u2/6) / np.pi


def limb_darkening_normed(u_ld, r):
    """
    Limb-darkened flux, normalized by the central flux.

    Parameters
    ----------
    u_ld : list
        Quadratic limb-darkening parameters
    r : float or `~numpy.ndarray`
        Radius in units of stellar radii.

    Returns
    -------
    f : float or `~numpy.ndarray`
        Normalized flux at ``r``
    """
    return limb_darkening(u_ld, r)/limb_darkening(u_ld, 0)


def total_flux(u_ld):
    """
    Compute the total flux of the limb-darkened star.

    Parameters
    ----------
    u_ld : list
        Quadratic limb-darkening parameters

    Returns
    -------
    f : float
        Total flux
    """
    return 2 * np.pi * quad(lambda r: r * limb_darkening_normed(u_ld, r),
                            0, 1)[0]


def ellipse(center, lengths, angle=0):
    """
    Create a shapely ellipse.

    Parameters
    ----------
    center : list
        [x, y] centroid of the ellipse
    lengths : list
        [a, b] semimajor and semiminor axes
    angle : float
        Angle in degrees to rotate the semimajor axis

    Returns
    -------
    ellipse : `~shapely.geometry.polygon.Polygon`
        Elliptical shapely object
    """
    ell = affinity.scale(Point(center).buffer(1),
                         xfact=lengths[0], yfact=lengths[1])
    ell_rotated = affinity.rotate(ell, angle=angle)
    return ell_rotated


def circle(center, radius):
    """
    Create a shapely ellipse.

    Parameters
    ----------
    center : list
        [x, y] centroid of the ellipse
    radius : float
        Radius of the circle

    Returns
    -------
    circle : `~shapely.geometry.polygon.Polygon`
        Circular shapely object
    """
    circle = affinity.scale(Point(center).buffer(1),
                            xfact=radius, yfact=radius)
    return circle


def consecutive(data, step_size=1):
    """
    Identify groups of consecutive integers, split them into separate arrays.
    """
    return np.split(data, np.where(np.diff(data) != step_size)[0]+1)


class Star(object):
    """
    Object describing properties of a (population of) star(s)
    """
    def __init__(self, spot_contrast, u_ld, phases=None, n_phases=None,
                 rotation_period=None):
        """
        Parameters
        ----------
        spot_contrast : float
            Contrast of spots (0=perfectly dark, 1=same as photosphere)
        u_ld : list
            Quadratic limb-darkening parameters
        n_phases : int, optional
            Number of rotation steps to iterate over
        phases : `~numpy.ndarray`, optional
            Rotational phases of the star
        rotation_period : `~astropy.units.Quantity`, optional
            Rotation period of the star
        """
        self.spot_contrast = spot_contrast
        if phases is not None:
            n_phases = len(phases)
        self.n_phases = n_phases
        self.u_ld = u_ld

        if phases is None and self.n_phases is not None:
            phases = np.arange(0, 2 * np.pi, 2 * np.pi / self.n_phases) * u.rad

        self.phases = phases
        self.f0 = total_flux(u_ld)
        self.rotation_period = rotation_period

    def light_curve(self, spot_lons, spot_lats, spot_radii, inc_stellar,
                    planet=None, times=None):
        """
        Generate a(n ensemble of) light curve(s).

        Light curve output will have shape ``(n_phases, len(inc_stellar))`` or
        ``(len(times), len(inc_stellar))``.

        Parameters
        ----------
        spot_lons : `~numpy.ndarray`
            Spot longitudes
        spot_lats : `~numpy.ndarray`
            Spot latitudes
        spot_radii : `~numpy.ndarray`
            Spot radii
        inc_stellar : `~numpy.ndarray`
            Stellar inclinations
        planet : `~batman.TransitParams`
            Transiting planet parameters
        times : `~numpy.ndarray`
            Times at which to compute the light curve

        Returns
        -------
        light_curves : `~numpy.ndarray`
            Stellar light curves of shape ``(n_phases, len(inc_stellar))`` or
            ``(len(times), len(inc_stellar))``
        """
        # Compute the spot positions in cartesian coordinates:
        tilted_spots = self.spot_params_to_cartesian(spot_lons, spot_lats,
                                                     inc_stellar, times=times,
                                                     planet=planet)

        # Compute the distance of each spot from the stellar centroid, mask
        # any spots that are "behind" the star, in other words, x < 0
        r = np.ma.masked_array(np.hypot(tilted_spots.y.value,
                                        tilted_spots.z.value),
                               mask=tilted_spots.x.value < 0)
        ld = limb_darkening_normed(self.u_ld, r)

        # Compute the out-of-transit flux missing due to each spot
        f_spots = (np.pi * spot_radii**2 * (1 - self.spot_contrast) * ld *
                   np.sqrt(1 - r**2))

        if planet is None:
            # If there is no transiting planet, skip the transit routine:
            lambda_e = np.zeros((len(self.phases), 1))
        else:
            if not inc_stellar.isscalar:
                raise ValueError('Transiting exoplanets are implemented for '
                                 'planets transiting single stars only, but '
                                 '``inc_stellar`` has multiple values. ')
            # Compute a transit model
            n_spots = len(spot_lons)
            m = TransitModel(planet, times)
            lambda_e = 1 - m.light_curve(planet)[:, np.newaxis]
            # Compute the true anomaly of the planet at each time, f:
            f = m.get_true_anomaly()

            # Compute the position of the planet in cartesian coordinates using
            # Equations 53-55 of Murray & Correia (2010). Note that these
            # coordinates are different from the cartesian coordinates used for
            # the spot positions. In this system, the observer is at X-> -inf.
            I = np.radians(90 - planet.inc)
            Omega = np.radians(planet.w)  # this is 90 deg by default
            omega = np.pi / 2
            X = planet.a * (np.cos(Omega) * np.cos(omega + f) -
                            np.sin(Omega) * np.sin(omega + f) * np.cos(I))
            Y = planet.a * (np.sin(Omega) * np.cos(omega + f) +
                            np.cos(Omega) * np.sin(omega + f) * np.cos(I))
            Z = planet.a * np.sin(omega + f) * np.sin(I)

            # Create a shapely circle object for the planet's silhouette only
            # when the planet is in front of the star, otherwise append `None`
            planet_disk = [circle([Y[i], Z[i]], planet.rp)
                           if (np.abs(Y[i]) < 1 + planet.rp) and
                              (X[i] < 0) else None
                           for i in range(len(f))]

            # Find the approximate mid-transit time indices in the observations
            # by looking for the sign flip in Y (planet crosses the sub-observer
            # point) when also X < 0 (planet in front of star):
            t0_inds = np.argwhere((np.sign(Y[1:]) < np.sign(Y[:-1])) &
                                  (X[1:] < 0))

            # Compute the indices where the planet is in front of the star
            # (X < 0) and the planet is near the star |Y| < 1 + p:
            transit_inds_all = np.argwhere((X < 0) &
                                           (np.abs(Y) < 1 + planet.rp))[:, 0]

            # Split these indices up into separate numpy arrays for each
            # contiguous group - this will generate a list of numpy arrays each
            # containing the indices during individual transit events.
            transit_inds_groups = consecutive(transit_inds_all)

            # For each transit in the observations:
            for k, t0_ind, transit_inds in zip(range(len(t0_inds)), t0_inds,
                                               transit_inds_groups):

                spots = []
                spot_ld_factors = []

                for i in range(n_spots):
                    # If the spot is visible (x > 0):
                    if tilted_spots.x.value[t0_ind, i] > 0:
                        spot_y = tilted_spots.y.value[t0_ind, i]
                        spot_z = tilted_spots.z.value[t0_ind, i]

                        # Compute the spot position and ellipsoidal shape
                        r_spot = np.hypot(spot_z, spot_y)
                        angle = np.arctan2(spot_z, spot_y)
                        ellipse_centroid = [spot_y, spot_z]

                        ellipse_axes = [spot_radii[i, 0] *
                                        np.sqrt(1 - r_spot**2),
                                        spot_radii[i, 0]]

                        spot = ellipse(ellipse_centroid, ellipse_axes,
                                       np.degrees(angle))

                        # Add the spot to our spot list
                        spots.append(spot)
                        spot_ld_factors.append(limb_darkening_normed(self.u_ld,
                                                                     r_spot))

                # If any spots are visible:
                if len(spots) > 0:
                    intersections = np.zeros((transit_inds.ptp()+1, len(spots)))

                    # For each time when the planet is nearly transiting:
                    for i in range(len(transit_inds)):
                        planet_disk_i = planet_disk[transit_inds[i]]
                        if planet_disk_i is not None:
                            for j in range(len(spots)):

                                # Compute the overlap between each spot and the
                                # planet using shapely's `intersection` method
                                spot_planet_overlap = planet_disk_i.intersection(spots[j]).area

                                intersections[i, j] = ((1 - self.spot_contrast) /
                                                       spot_ld_factors[j] *
                                                       spot_planet_overlap /
                                                       np.pi)

                    # Subtract the spot occultation amplitudes from the spotless
                    # transit model that we computed earlier
                    lambda_e[transit_inds] -= intersections.max(axis=1)[:, np.newaxis]

        # Return the flux missing from the star at each time due to spots
        # (f_spots/self.f0) and due to the transit (lambda_e):
        return 1 - np.sum(f_spots.filled(0)/self.f0, axis=1) - lambda_e

    def plot(self, spot_lons, spot_lats, spot_radii, inc_stellar, time=None,
             planet=None, ax=None):
        """
        Generate a plot of the stellar surface at ``time``.

        Takes the same arguments as `~fleck.light_curve` with the exception of
        the singular ``time`` rather than ``times``, plus ``ax`` for pre-defined
        matplotlib axes.

        Parameters
        ----------
        spot_lons : `~astropy.units.Quantity`
            Spot longitudes
        spot_lats : `~astropy.units.Quantity`
            Spot latitudes
        spot_radii : `~numpy.ndarray`
            Spot radii
        inc_stellar : `~astropy.units.Quantity`
            Stellar inclination
        time : float
            Time at which to evaluate the spot parameters
        planet : `~batman.TransitParams`
            Planet parameters
        ax : `~matplotlib.pyplot.Axes`, optional
            Predefined matplotlib axes

        Returns
        -------
        ax : `~matplotlib.pyplot.Axes`
            Axis object.
        """
        tilted_spots = self.spot_params_to_cartesian(spot_lons, spot_lats,
                                                     inc_stellar,
                                                     times=np.array([time]),
                                                     planet=planet)
        spots = []

        for i in range(len(spot_lons)):
            # If the spot is visible (x > 0):
            if tilted_spots.x.value[0, i] > 0:
                spot_y = tilted_spots.y.value[0, i]
                spot_z = tilted_spots.z.value[0, i]

                # Compute the spot position and ellipsoidal shape
                r_spot = np.hypot(spot_z, spot_y)
                angle = np.arctan2(spot_z, spot_y)
                ellipse_centroid = [spot_y, spot_z]

                ellipse_axes = [spot_radii[i, 0] *
                                np.sqrt(1 - r_spot**2),
                                spot_radii[i, 0]]

                spot = ellipse(ellipse_centroid, ellipse_axes,
                               np.degrees(angle))

                # Add the spot to our spot list
                spots.append(spot)

        if ax is None:
            ax = plt.gca()

        # Calculate impact parameter
        b = (planet.a * np.cos(np.radians(planet.inc)) * (1 - planet.ecc**2) /
             (1 + planet.ecc * np.sin(np.radians(planet.w))))

        planet_lower_extent = -b-planet.rp
        planet_upper_extent = -b+planet.rp

        # Draw the outline of the star:
        x = np.linspace(-1, 1, 1000)
        ax.plot(x, np.sqrt(1-x**2), color='k')
        ax.plot(x, -np.sqrt(1-x**2), color='k')
        ax.axhline(planet_lower_extent, color='gray', ls='--')
        ax.axhline(planet_upper_extent, color='gray', ls='--')
        ax.set(ylim=[-1, 1], xlim=[-1, 1], aspect=1)

        # Draw each starspot:
        for i in range(len(spots)):
            x, y = [np.array(j.tolist()) for j in spots[i].exterior.xy]
            ax.fill(-x, y, alpha=1-self.spot_contrast,
                    color='k')
        return ax

    def spot_params_to_cartesian(self, spot_lons, spot_lats, inc_stellar,
                                 times=None, planet=None):
        """
        Convert spot parameter matrices in the original stellar coordinates to
        rotated and tilted cartesian coordinates.

        Parameters
        ----------
        spot_lons : `~astropy.units.Quantity`
            Spot longitudes
        spot_lats : `~astropy.units.Quantity`
            Spot latitudes
        inc_stellar : `~astropy.units.Quantity`
            Stellar inclination
        times : `~numpy.ndarray`
            Times at which evaluate the stellar rotation
        planet : `~batman.TransitParams`
            Planet parameters
        Returns
        -------
        tilted_spots : `~numpy.ndarray`
            Rotated and tilted spot positions in cartesian coordinates
        """
        # Spots by default are given in unit spherical representation (lat, lon)
        usr = UnitSphericalRepresentation(spot_lons, spot_lats)

        # Represent those spots with cartesian coordinates (x, y, z)
        # In this coordinate system, the observer is at positive x->inf,
        # the star is at the origin, and (y, z) is the sky plane.
        cartesian = usr.represent_as(CartesianRepresentation)

        # Generate array of rotation matrices to rotate the spots about the
        # stellar rotation axis
        if times is None:
            rotate = rotation_matrix(self.phases[:, np.newaxis, np.newaxis],
                                     axis='z')
        else:
            rotational_phase = 2 * np.pi * ((times - planet.t0) /
                                            self.rotation_period) * u.rad
            rotate = rotation_matrix(rotational_phase[:, np.newaxis, np.newaxis],
                                     axis='z')

        rotated_spots = cartesian.transform(rotate)

        if planet is not None and hasattr(planet, 'lam'):
            lam = planet.lam * u.deg
        else:
            lam = 0 * u.deg

        # Generate array of rotation matrices to rotate the spots so that the
        # star is observed from the correct stellar inclination
        stellar_inclination = rotation_matrix(inc_stellar - 90*u.deg, axis='y')
        inclined_spots = rotated_spots.transform(stellar_inclination)

        # Generate array of rotation matrices to rotate the spots so that the
        # planet's orbit normal is tilted with respect to stellar spin
        tilt = rotation_matrix(lam, axis='x')
        tilted_spots = inclined_spots.transform(tilt)

        return tilted_spots


def generate_spots(min_latitude, max_latitude, spot_radius, n_spots,
                   n_inclinations=None, inclinations=None):
    """
    Generate matrices of spot parameters.

    Will generate ``n_spots`` spots on different stars observed at
    ``n_inclinations`` different inclinations.

    Parameters
    ----------
    min_latitude : float
        Minimum spot latitude
    max_latitude : float
        Maximum spot latitude
    spot_radius : float or `~numpy.ndarray`
        Spot radii
    n_spots : int
        Number of spots to generate
    n_inclinations : int, optional
        Number of inclinations to generate
    inclinations : `~numpy.ndarray`, optional
        Inclinations (user defined). Default (`None`): randomly generate.

    Returns
    -------
    lons : `~astropy.units.Quantity`
        Spot longitudes, shape ``(n_spots, n_inclinations)``
    lats : `~astropy.units.Quantity`
        Spot latitudes, shape ``(n_spots, n_inclinations)``
    radii : float or `~numpy.ndarray`
        Spot radii, shape ``(n_spots, n_inclinations)``
    inc_stellar : `~astropy.units.Quantity`
        Stellar inclinations, shape ``(n_inclinations, )``
    """
    delta_latitude = max_latitude - min_latitude
    if n_inclinations is not None and inclinations is None:
        inc_stellar = (180*np.random.rand(n_inclinations) - 90) * u.deg
    else:
        n_inclinations = len(inclinations)
        inc_stellar = inclinations
    radii = spot_radius * np.ones((n_spots, n_inclinations))
    lats = (delta_latitude*np.random.rand(n_spots, n_inclinations) +
            min_latitude) * u.deg
    lons = 360*np.random.rand(n_spots, n_inclinations) * u.deg
    return lons, lats, radii, inc_stellar
