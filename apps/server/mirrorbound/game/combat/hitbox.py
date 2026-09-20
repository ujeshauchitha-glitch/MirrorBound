"""Hitbox shapes for hit detection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from mirrorbound.game.entities.entity import Vec2


class HitboxShape(Enum):
    """Types of hitboxes."""
    CIRCLE = "circle"
    ARC = "arc"
    LINE = "line"


@dataclass
class Hitbox:
    """Hitbox for hit detection."""
    shape: HitboxShape
    position: Vec2
    size: float  # Radius for circle, width for arc, length for line
    direction: float = 0.0  # Angle in radians for arc/line
    arc_angle: float = 1.57  # PI/2 for 90 degree arc

    def contains_point(self, point: Vec2) -> bool:
        """Check if a point is inside this hitbox."""
        if self.shape == HitboxShape.CIRCLE:
            return self._point_in_circle(point)
        elif self.shape == HitboxShape.ARC:
            return self._point_in_arc(point)
        elif self.shape == HitboxShape.LINE:
            return self._point_in_line(point)
        return False

    def overlaps_circle(self, center: Vec2, radius: float) -> bool:
        """Check if this hitbox overlaps with a circle."""
        if self.shape == HitboxShape.CIRCLE:
            dist = (self.position - center).length()
            return dist < (self.size + radius)
        elif self.shape == HitboxShape.ARC:
            dist = (self.position - center).length()
            if dist > self.size + radius:
                return False
            # Standing inside us: there is no meaningful direction to it.
            if dist <= radius:
                return True
            # A body has angular width, not just a direction.
            #
            # The distance test above already accounts for `radius`; the angle
            # test used to compare only the direction of the target's *centre*
            # against the arc, which quietly meant every enemy was a point.
            # A circle of `radius` at `dist` subtends asin(radius / dist)
            # either side of its centre, so a target whose centre sits outside
            # the arc can still have most of its body inside it -- and the
            # wider the enemy, the more of the swing went missing. Measured
            # with bare hands (a 97 degree arc, so 49 either side): every
            # archetype cut off at exactly 48 degrees regardless of size, and
            # a Husk Scarab -- whose body spans 53 degrees at melee range --
            # lost 62 degrees of swing.
            slack = math.asin(min(1.0, radius / dist))
            offset = abs(self._angle_difference(self._angle_to(center), self.direction))
            return offset <= self.arc_angle / 2 + slack
        elif self.shape == HitboxShape.LINE:
            # Simplified: check distance from line
            dist = self._point_to_line_distance(center)
            return dist < radius
        return False

    def _point_in_circle(self, point: Vec2) -> bool:
        """Check if point is inside circle hitbox."""
        dist = (self.position - point).length()
        return dist <= self.size

    def _point_in_arc(self, point: Vec2) -> bool:
        """Check if point is inside arc hitbox."""
        dist = (self.position - point).length()
        if dist > self.size:
            return False
        angle_to_point = self._angle_to(point)
        return self._angle_in_arc(angle_to_point)

    def _point_in_line(self, point: Vec2) -> bool:
        """Check if point is near line hitbox."""
        dist = self._point_to_line_distance(point)
        return dist <= self.size

    def _angle_to(self, point: Vec2) -> float:
        """Calculate angle from hitbox position to point."""
        dx = point.x - self.position.x
        dy = point.y - self.position.y
        return self._normalize_angle(self._atan2(dy, dx))

    @staticmethod
    def _angle_difference(a: float, b: float) -> float:
        """Signed shortest angle from `b` to `a`, in (-PI, PI]."""
        return (a - b + math.pi) % (2 * math.pi) - math.pi

    def _angle_in_arc(self, angle: float) -> bool:
        """Check if angle is within the arc."""
        start = self._normalize_angle(self.direction - self.arc_angle / 2)
        end = self._normalize_angle(self.direction + self.arc_angle / 2)

        if start <= end:
            return start <= angle <= end
        else:
            # Arc wraps around 0
            return angle >= start or angle <= end

    def _point_to_line_distance(self, point: Vec2) -> float:
        """Calculate distance from point to line."""
        # Line end point
        end = Vec2(
            self.position.x + self._cos(self.direction) * self.size,
            self.position.y + self._sin(self.direction) * self.size,
        )

        # Project point onto line
        line_vec = end - self.position
        point_vec = point - self.position
        line_len = line_vec.length()

        if line_len == 0:
            return point_vec.length()

        t = max(0, min(1, (point_vec.x * line_vec.x + point_vec.y * line_vec.y) / (line_len * line_len)))
        projection = Vec2(
            self.position.x + t * line_vec.x,
            self.position.y + t * line_vec.y,
        )

        return (point - projection).length()

    def _normalize_angle(self, angle: float) -> float:
        """Normalize angle to [0, 2*PI]."""
        import math
        while angle < 0:
            angle += 2 * math.pi
        while angle >= 2 * math.pi:
            angle -= 2 * math.pi
        return angle

    @staticmethod
    def _atan2(y: float, x: float) -> float:
        import math
        return math.atan2(y, x)

    @staticmethod
    def _cos(angle: float) -> float:
        import math
        return math.cos(angle)

    @staticmethod
    def _sin(angle: float) -> float:
        import math
        return math.sin(angle)
