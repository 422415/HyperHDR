#pragma once

#ifndef PCH_ENABLED
	#include <cassert>
	#include <sstream>
	#include <math.h>
	#include <algorithm>
	#include <vector>
	#include <map>
#endif

#include <image/Image.h>
#include <utils/Logger.h>
#include <base/LedString.h>

#include <linalg.h>

namespace hyperhdr
{	
	struct DominantColorConfig
	{
		bool brightNeutralSuppression = true;
		float brightNeutralMaxCoverage = 0.12f;
		float brightNeutralMinLuma = 0.50f;
		float brightNeutralMaxSaturation = 0.20f;
		float darkSceneMaxLuma = 0.35f;
		bool oklchNeutralSceneProtection = true;
		float oklchNeutralSceneMinCoverage = 0.75f;
		float oklchNeutralSceneMaxSaturation = 0.12f;
		float oklchNeutralSceneMinLuma = 0.45f;
		float oklchMinColoredCoverage = 0.08f;
		float oklchDominantBrightnessBlend = 0.70f;

		bool operator!=(const DominantColorConfig& other) const
		{
			return brightNeutralSuppression != other.brightNeutralSuppression ||
				brightNeutralMaxCoverage != other.brightNeutralMaxCoverage ||
				brightNeutralMinLuma != other.brightNeutralMinLuma ||
				brightNeutralMaxSaturation != other.brightNeutralMaxSaturation ||
				darkSceneMaxLuma != other.darkSceneMaxLuma ||
				oklchNeutralSceneProtection != other.oklchNeutralSceneProtection ||
				oklchNeutralSceneMinCoverage != other.oklchNeutralSceneMinCoverage ||
				oklchNeutralSceneMaxSaturation != other.oklchNeutralSceneMaxSaturation ||
				oklchNeutralSceneMinLuma != other.oklchNeutralSceneMinLuma ||
				oklchMinColoredCoverage != other.oklchMinColoredCoverage ||
				oklchDominantBrightnessBlend != other.oklchDominantBrightnessBlend;
		}
	};

	class ImageColorAveraging
	{
	public:
		ImageColorAveraging(
			const LoggerName& _log,
			const int mappingType,
			const bool sparseProcessing,
			const unsigned width,
			const unsigned height,
			const unsigned horizontalBorder,
			const unsigned verticalBorder,
			const quint8 instanceIndex,
			const DominantColorConfig& dominantColorConfig,
			const std::vector<LedString::Led>& leds);

		unsigned width() const;
		unsigned height() const;

		unsigned horizontalBorder() const;
		unsigned verticalBorder() const;

		void process(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image);

	private:
		void getUnicolorForLeds(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image) const;
		void getMulticolorForLeds(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image) const;
		void getVividMulticolorForLeds(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image) const;
		void getDominantMulticolorForLeds(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image) const;
		void getDominantOklchMulticolorForLeds(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& image) const;

		const unsigned _width;
		const unsigned _height;
		const bool	_sparseProcessing;
		const unsigned _horizontalBorder;
		const unsigned _verticalBorder;
		int _mappingType;
		DominantColorConfig _dominantColorConfig;

		std::vector<std::vector<uint32_t>> _colorsMap;
		std::map<int, std::vector<uint32_t>> _colorGroups;

		linalg::aliases::float3 calcMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const;
		linalg::aliases::float3 calcVividMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const;
		linalg::aliases::float3 calcDominantMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const;
		linalg::aliases::float3 calcDominantOklchMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const;
		linalg::aliases::float3 calcUnicolorForLeds(const Image<ColorRgb>& image) const;
	};
}
