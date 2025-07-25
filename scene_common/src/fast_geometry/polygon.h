/*
 * SPDX-FileCopyrightText: (C) 2024 - 2025 Intel Corporation
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef REGION_H
#define REGION_H

#include <string>
#include <map>
#include <vector>

class Polygon
{
  public:

    Polygon(const std::vector<std::pair<double, double>>& vertices);

    std::vector<std::pair<double, double>> getVertices() const ;

    // Method to check if a point is inside the region
    bool isPointInside(double px, double py) const ;

  private:
    std::vector<std::pair<double, double>> vertices;
    int region_type;
};


#endif
