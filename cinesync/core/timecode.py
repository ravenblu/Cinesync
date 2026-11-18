"""Strict, explicit embedded shoot-timecode evidence."""
from fractions import Fraction

from .engine import ValidationError, validate_fps, smpte_to_frames


def rational(value: object) -> Fraction:
    """Read a JSON-safe fraction or a finite numeric value."""
    try:
        if isinstance(value, bool):
            raise ValueError('Boolean time')
        if isinstance(value, dict):
            numerator = value['numerator']
            denominator = value['denominator']
            if type(numerator) is not int or type(denominator) is not int:
                raise ValueError('Fraction components must be integers')
            return Fraction(numerator, denominator)
        return Fraction(str(value))
    except (ValueError, TypeError, ZeroDivisionError, KeyError) as exc:
        raise ValidationError(f'Invalid frame value: {value!r}') from exc


def packed(value: Fraction) -> dict:
    return {'numerator': value.numerator, 'denominator': value.denominator}


def validate_timecode(value: dict) -> dict:
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValidationError('Missing or unsupported embedded FBX shoot timecode')
    try:
        fps = validate_fps(value['fps'])
        smpte_to_frames(value['value'], fps)
        local = packed(rational(value['local_frame']))
    except (KeyError, TypeError) as exc:
        raise ValidationError('Incomplete embedded FBX shoot timecode') from exc
    if not isinstance(value.get('origin'), str) or not value['origin']:
        raise ValidationError('Missing timecode metadata origin')
    return dict(value, fps=fps, local_frame=local)
