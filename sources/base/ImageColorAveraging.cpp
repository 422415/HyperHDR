/* ImageColorAveraging.cpp
*
*  MIT License
*
*  Copyright (c) 2020-2026 awawa-dev
*
*  Project homesite: https://github.com/awawa-dev/HyperHDR
*
*  Permission is hereby granted, free of charge, to any person obtaining a copy
*  of this software and associated documentation files (the "Software"), to deal
*  in the Software without restriction, including without limitation the rights
*  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
*  copies of the Software, and to permit persons to whom the Software is
*  furnished to do so, subject to the following conditions:
*
*  The above copyright notice and this permission notice shall be included in all
*  copies or substantial portions of the Software.

*  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
*  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
*  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
*  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
*  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
*  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
*  SOFTWARE.
*/

#include <base/ImageColorAveraging.h>
#include <base/ImageToLedManager.h>
#include <infinite-color-engine/ColorSpace.h>
#include <infinite-color-engine/InfiniteProcessing.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <ranges>
#include <iterator>

#define push_back_index(list, index) list.push_back((index) * 3)

using namespace hyperhdr;
using namespace linalg::aliases;

namespace
{
	constexpr int DOMINANT_HUE_BINS = 24;
	constexpr float OKLCH_PI = 3.14159265358979323846f;
	constexpr float OKLCH_MIN_CHROMA = 0.015f;
	constexpr float OKLCH_CHROMA_REFERENCE = 0.12f;
	constexpr float OKLCH_COLORFULNESS_BOOST = 1.15f;
	constexpr float OKLCH_MIN_DOMINANT_SHARE = 0.16f;
	constexpr float OKLCH_MIN_HUE_COHERENCE = 0.18f;

	float3 clampFloat3(const float3& value, float minValue, float maxValue)
	{
		return float3{
			std::clamp(value.x, minValue, maxValue),
			std::clamp(value.y, minValue, maxValue),
			std::clamp(value.z, minValue, maxValue)
		};
	}

	float smoothStep(float edge0, float edge1, float value)
	{
		const float t = std::clamp((value - edge0) / (edge1 - edge0), 0.0f, 1.0f);
		return t * t * (3.0f - 2.0f * t);
	}

	bool isBrightNeutralHighlight(const DominantColorConfig& config, float maxComponent, float saturation, float luma)
	{
		return luma >= config.brightNeutralMinLuma &&
			maxComponent >= config.brightNeutralMinLuma &&
			saturation <= config.brightNeutralMaxSaturation;
	}

	int colorToHueBin(const float3& color, float maxComponent, float chroma)
	{
		if (chroma <= 0.0001f)
			return 0;

		float hue = 0.0f;
		if (maxComponent == color.x)
		{
			hue = (color.y - color.z) / chroma;
			if (hue < 0.0f)
				hue += 6.0f;
		}
		else if (maxComponent == color.y)
		{
			hue = ((color.z - color.x) / chroma) + 2.0f;
		}
		else
		{
			hue = ((color.x - color.y) / chroma) + 4.0f;
		}

		return std::clamp(static_cast<int>((hue / 6.0f) * DOMINANT_HUE_BINS), 0, DOMINANT_HUE_BINS - 1);
	}

	int oklchHueToBin(float hue)
	{
		const float normalized = (hue + OKLCH_PI) / (2.0f * OKLCH_PI);
		return std::clamp(static_cast<int>(normalized * DOMINANT_HUE_BINS), 0, DOMINANT_HUE_BINS - 1);
	}
}

ImageColorAveraging::ImageColorAveraging(
				const LoggerName& _log,
				const int mappingType,
				const bool sparseProcessing,
				const unsigned width,
				const unsigned height,
				const unsigned horizontalBorder,
				const unsigned verticalBorder,
				const quint8 /*instanceIndex*/,
				const DominantColorConfig& dominantColorConfig,
				const std::vector<LedString::Led>& leds)
	: _width(width)
	, _height(height)
	, _sparseProcessing(sparseProcessing)
	, _horizontalBorder(horizontalBorder)
	, _verticalBorder(verticalBorder)
	, _dominantColorConfig(dominantColorConfig)
	, _colorsMap()
	, _colorGroups()
{
	Q_ASSERT(_width > 2 * _verticalBorder);
	Q_ASSERT(_height > 2 * _horizontalBorder);
	Q_ASSERT(_width < 10000);
	Q_ASSERT(_height < 10000);

	_mappingType = mappingType;

	_colorsMap.reserve(leds.size());

	const int32_t xOffset = _verticalBorder;
	const int32_t actualWidth = _width - 2 * _verticalBorder;
	const int32_t yOffset = _horizontalBorder;
	const int32_t actualHeight = _height - 2 * _horizontalBorder;

	size_t   totalCount = 0;
	size_t   totalCapasity = 0;

	for (int ledIndex = -1; const LedString::Led& led : leds)
	{
		ledIndex++;

		if ((led.maxX_frac - led.minX_frac) < 1e-6 || (led.maxY_frac - led.minY_frac) < 1e-6)
		{
			_colorsMap.emplace_back();
			continue;
		}

		int32_t minX_idx = xOffset + int32_t(qRound(actualWidth * led.minX_frac));
		int32_t maxX_idx = xOffset + int32_t(qRound(actualWidth * led.maxX_frac));
		int32_t minY_idx = yOffset + int32_t(qRound(actualHeight * led.minY_frac));
		int32_t maxY_idx = yOffset + int32_t(qRound(actualHeight * led.maxY_frac));

		minX_idx = qMin(minX_idx, xOffset + actualWidth - 1);
		if (minX_idx == maxX_idx)
		{
			maxX_idx++;
		}
		minY_idx = qMin(minY_idx, yOffset + actualHeight - 1);
		if (minY_idx == maxY_idx)
		{
			maxY_idx++;
		}

		const int32_t maxYLedCount = qMin(maxY_idx, yOffset + actualHeight);
		const int32_t maxXLedCount = qMin(maxX_idx, xOffset + actualWidth);
		const int32_t realYLedCount = qAbs(maxYLedCount - minY_idx);
		const int32_t realXLedCount = qAbs(maxXLedCount - minX_idx);

		bool   sparseIndexes = sparseProcessing;
		size_t totalSize = static_cast<size_t>(realYLedCount) * realXLedCount;

		if (!sparseIndexes && totalSize > 1600)
		{
			Warning(_log, "This is large image area for lamp: {:d}. It contains {:d} indexes for captured video frame so reduce it by four. Enabling 'sparse processing' option for you. Consider to enable it permanently in the processing configuration to hide that warning.", ledIndex, totalSize);
			sparseIndexes = true;
		}

		const int32_t increment = (sparseIndexes) ? 2 : 1;

		totalCount += _colorsMap.emplace_back([&]{
			std::vector<uint32_t> ledColor;
			if (!led.disabled)
			{
				ledColor.reserve((sparseIndexes) ? ((static_cast<unsigned long long>(realYLedCount / 2)) + (realYLedCount % 2)) * ((realXLedCount / 2) + (realXLedCount % 2)) : totalSize);

				for (int32_t y = minY_idx; y < maxYLedCount; y += increment)
				{
					for (int32_t x = minX_idx; x < maxXLedCount; x += increment)
					{
						push_back_index(ledColor, y * width + x);
					}
				}
			}
			return ledColor;
		}()).size();
		totalCapasity += _colorsMap.back().capacity();

		if (led.group > 0)
		{
			if (_colorGroups.contains(led.group))
			{
				int master = _colorGroups[led.group].front();
				auto& dest_vec = _colorsMap[master];
				auto& source_vec = _colorsMap.back();

				dest_vec.insert(
					dest_vec.end(),
					std::make_move_iterator(source_vec.begin()),
					std::make_move_iterator(source_vec.end())
				);

				source_vec.clear();
			}
			_colorGroups[led.group].push_back(ledIndex);
		}
	}
	Info(_log, "Total index number is: {:d} (memory: {:d}). User sparse processing is: {:s}, image size: {:d} x {:d}, area number: {:d}",
		totalCount, totalCapasity, (sparseProcessing) ? "enabled" : "disabled", width, height, leds.size());
}

unsigned ImageColorAveraging::width() const
{
	return _width;
}

unsigned ImageColorAveraging::height() const
{
	return _height;
}

unsigned ImageColorAveraging::horizontalBorder() const
{
	return _horizontalBorder;
}

unsigned ImageColorAveraging::verticalBorder() const {
	return _verticalBorder;
}

void ImageColorAveraging::process(std::vector<float3>& ledColors, const Image<ColorRgb>& image)
{
	ledColors.clear();
	ledColors.reserve(_colorsMap.size());

	switch (_mappingType)
	{
		case 1: getUnicolorForLeds(ledColors, image); break;
		case 2: getVividMulticolorForLeds(ledColors, image); break;
		case 3: getDominantMulticolorForLeds(ledColors, image); break;
		case 4: getDominantOklchMulticolorForLeds(ledColors, image); break;
		default: getMulticolorForLeds(ledColors, image);
	}

	if (!_colorGroups.empty() && _mappingType != 1)
	{
		for (auto& group : _colorGroups)
		{
			const auto& combined = ledColors[group.second.front()];
			for (int g = 1; g < static_cast<int>(group.second.size()); g++)
			{
				ledColors[group.second[g]] = combined;
			}
		}
	}
}

void  ImageColorAveraging::getUnicolorForLeds(std::vector<float3>& ledColors, const Image<ColorRgb>& image) const
{
	ledColors.resize(_colorsMap.size(), calcUnicolorForLeds(image));
}


void ImageColorAveraging::getMulticolorForLeds(std::vector<float3>& ledColors, const Image<ColorRgb>& image) const
{
	for (auto colors = _colorsMap.begin(); colors != _colorsMap.end(); ++colors)
	{
		ledColors.push_back(calcMulticolorForLeds(image, *colors));
	}
}

void ImageColorAveraging::getVividMulticolorForLeds(std::vector<float3>& ledColors, const Image<ColorRgb>& image) const
{
	for (auto colors = _colorsMap.begin(); colors != _colorsMap.end(); ++colors)
	{
		ledColors.push_back(calcVividMulticolorForLeds(image, *colors));
	}
}

void ImageColorAveraging::getDominantMulticolorForLeds(std::vector<float3>& ledColors, const Image<ColorRgb>& image) const
{
	for (auto colors = _colorsMap.begin(); colors != _colorsMap.end(); ++colors)
	{
		ledColors.push_back(calcDominantMulticolorForLeds(image, *colors));
	}
}

void ImageColorAveraging::getDominantOklchMulticolorForLeds(std::vector<float3>& ledColors, const Image<ColorRgb>& image) const
{
	for (auto colors = _colorsMap.begin(); colors != _colorsMap.end(); ++colors)
	{
		ledColors.push_back(calcDominantOklchMulticolorForLeds(image, *colors));
	}
}

float3 ImageColorAveraging::calcMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const
{
	if (colors.empty())
	{
		return float3{ 0, 0, 0 };
	}

	linalg::vec<uint_fast64_t, 3> sumLinear(0, 0, 0);

	const uint8_t* imgData = image.rawMem();

	for (const uint32_t colorOffset : colors)
	{		
		sumLinear += InfiniteProcessing::srgbNonlinearToLinear(byte3(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]));
	}

	auto averageLinear = (static_cast<float3>(sumLinear) / static_cast<float>(colors.size())) / 65535.0f;

	return averageLinear;
}

float3 ImageColorAveraging::calcVividMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const
{
	if (colors.empty())
	{
		return float3{ 0, 0, 0 };
	}

	const uint8_t* imgData = image.rawMem();
	float3 sumLinear(0, 0, 0);
	float sumWeight = 0.0f;

	for (const uint32_t colorOffset : colors)
	{
		const byte3 nonlinear(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]);
		const float3 nonlinear01 = static_cast<float3>(nonlinear) / 255.0f;
		const float maxComponent = linalg::maxelem(nonlinear01);
		const float minComponent = linalg::minelem(nonlinear01);
		const float chroma = maxComponent - minComponent;
		const float saturation = (maxComponent > 0.0001f) ? chroma / maxComponent : 0.0f;
		const float luma = 0.2126f * nonlinear01.x + 0.7152f * nonlinear01.y + 0.0722f * nonlinear01.z;
		const float visible = std::clamp((luma - 0.025f) / 0.18f, 0.0f, 1.0f);
		const float weight = 0.25f + 0.75f * visible + 3.0f * saturation * saturation * visible;

		sumLinear += (static_cast<float3>(InfiniteProcessing::srgbNonlinearToLinear(nonlinear)) / 65535.0f) * weight;
		sumWeight += weight;
	}

	return (sumWeight > 0.0001f) ? sumLinear / sumWeight : float3{ 0, 0, 0 };
}

float3 ImageColorAveraging::calcDominantMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const
{
	if (colors.empty())
	{
		return float3{ 0, 0, 0 };
	}

	const uint8_t* imgData = image.rawMem();
	std::array<float3, DOMINANT_HUE_BINS> hueSums{};
	std::array<float, DOMINANT_HUE_BINS> hueWeights{};
	float3 neutralSum(0, 0, 0);
	float3 ambientSum(0, 0, 0);
	float neutralWeight = 0.0f;
	float ambientWeight = 0.0f;
	float totalHueWeight = 0.0f;
	size_t brightNeutralHighlightCount = 0;
	size_t nonHighlightCount = 0;
	float nonHighlightLumaSum = 0.0f;

	for (const uint32_t colorOffset : colors)
	{
		const byte3 nonlinear(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]);
		const float3 nonlinear01 = static_cast<float3>(nonlinear) / 255.0f;
		const float maxComponent = linalg::maxelem(nonlinear01);
		const float minComponent = linalg::minelem(nonlinear01);
		const float chroma = maxComponent - minComponent;
		const float saturation = (maxComponent > 0.0001f) ? chroma / maxComponent : 0.0f;
		const float luma = 0.2126f * nonlinear01.x + 0.7152f * nonlinear01.y + 0.0722f * nonlinear01.z;

		if (isBrightNeutralHighlight(_dominantColorConfig, maxComponent, saturation, luma))
		{
			brightNeutralHighlightCount++;
		}
		else
		{
			nonHighlightCount++;
			nonHighlightLumaSum += luma;
		}
	}

	const float brightNeutralHighlightCoverage = static_cast<float>(brightNeutralHighlightCount) / static_cast<float>(colors.size());
	const float nonHighlightAverageLuma = (nonHighlightCount > 0) ? nonHighlightLumaSum / static_cast<float>(nonHighlightCount) : 1.0f;
	const bool suppressSparseBrightNeutralHighlights =
		_dominantColorConfig.brightNeutralSuppression &&
		brightNeutralHighlightCount > 0 &&
		brightNeutralHighlightCoverage <= _dominantColorConfig.brightNeutralMaxCoverage &&
		nonHighlightAverageLuma <= _dominantColorConfig.darkSceneMaxLuma;

	for (const uint32_t colorOffset : colors)
	{
		const byte3 nonlinear(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]);
		const float3 nonlinear01 = static_cast<float3>(nonlinear) / 255.0f;
		const float maxComponent = linalg::maxelem(nonlinear01);
		const float minComponent = linalg::minelem(nonlinear01);
		const float chroma = maxComponent - minComponent;
		const float saturation = (maxComponent > 0.0001f) ? chroma / maxComponent : 0.0f;
		const float luma = 0.2126f * nonlinear01.x + 0.7152f * nonlinear01.y + 0.0722f * nonlinear01.z;
		const float visible = std::clamp((luma - 0.025f) / 0.18f, 0.0f, 1.0f);
		const float3 linear = static_cast<float3>(InfiniteProcessing::srgbNonlinearToLinear(nonlinear)) / 65535.0f;

		if (suppressSparseBrightNeutralHighlights && isBrightNeutralHighlight(_dominantColorConfig, maxComponent, saturation, luma))
		{
			continue;
		}

		ambientSum += linear;
		ambientWeight += 1.0f;

		if (saturation > 0.08f)
		{
			const float hueWeight = visible * maxComponent * saturation * saturation;
			const int hueBin = colorToHueBin(nonlinear01, maxComponent, chroma);
			hueSums[hueBin] += linear * hueWeight;
			hueWeights[hueBin] += hueWeight;
			totalHueWeight += hueWeight;
		}

		const float neutralAmount = 1.0f - saturation;
		const float currentNeutralWeight = visible * maxComponent * neutralAmount * neutralAmount * 0.35f;
		neutralSum += linear * currentNeutralWeight;
		neutralWeight += currentNeutralWeight;
	}

	float3 bestHueSum(0, 0, 0);
	float bestHueWeight = 0.0f;
	for (int i = 0; i < DOMINANT_HUE_BINS; ++i)
	{
		const int prev = (i + DOMINANT_HUE_BINS - 1) % DOMINANT_HUE_BINS;
		const int next = (i + 1) % DOMINANT_HUE_BINS;
		const float candidateWeight = hueWeights[prev] + hueWeights[i] + hueWeights[next];
		if (candidateWeight > bestHueWeight)
		{
			bestHueWeight = candidateWeight;
			bestHueSum = hueSums[prev] + hueSums[i] + hueSums[next];
		}
	}

	const bool hasClearDominantHue =
		bestHueWeight > 0.0001f &&
		bestHueWeight >= neutralWeight * 0.35f &&
		(totalHueWeight <= 0.0001f || (bestHueWeight / totalHueWeight) >= 0.18f);

	if (hasClearDominantHue)
	{
		return bestHueSum / bestHueWeight;
	}

	if (neutralWeight > 0.0001f)
	{
		return neutralSum / neutralWeight;
	}

	if (ambientWeight > 0.0001f)
	{
		return ambientSum / ambientWeight;
	}

	return calcVividMulticolorForLeds(image, colors);
}

float3 ImageColorAveraging::calcDominantOklchMulticolorForLeds(const Image<ColorRgb>& image, const std::vector<uint32_t>& colors) const
{
	if (colors.empty())
	{
		return float3{ 0, 0, 0 };
	}

	const uint8_t* imgData = image.rawMem();
	size_t brightNeutralHighlightCount = 0;
	size_t nonHighlightCount = 0;
	float nonHighlightLumaSum = 0.0f;

	for (const uint32_t colorOffset : colors)
	{
		const byte3 nonlinear(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]);
		const float3 nonlinear01 = static_cast<float3>(nonlinear) / 255.0f;
		const float maxComponent = linalg::maxelem(nonlinear01);
		const float minComponent = linalg::minelem(nonlinear01);
		const float chroma = maxComponent - minComponent;
		const float saturation = (maxComponent > 0.0001f) ? chroma / maxComponent : 0.0f;
		const float luma = 0.2126f * nonlinear01.x + 0.7152f * nonlinear01.y + 0.0722f * nonlinear01.z;

		if (isBrightNeutralHighlight(_dominantColorConfig, maxComponent, saturation, luma))
		{
			brightNeutralHighlightCount++;
		}
		else
		{
			nonHighlightCount++;
			nonHighlightLumaSum += luma;
		}
	}

	const float brightNeutralHighlightCoverage = static_cast<float>(brightNeutralHighlightCount) / static_cast<float>(colors.size());
	const float nonHighlightAverageLuma = (nonHighlightCount > 0) ? nonHighlightLumaSum / static_cast<float>(nonHighlightCount) : 1.0f;
	const bool suppressSparseBrightNeutralHighlights =
		_dominantColorConfig.brightNeutralSuppression &&
		brightNeutralHighlightCount > 0 &&
		brightNeutralHighlightCoverage <= _dominantColorConfig.brightNeutralMaxCoverage &&
		nonHighlightAverageLuma <= _dominantColorConfig.darkSceneMaxLuma;

	std::array<float, DOMINANT_HUE_BINS> hueWeights{};
	std::array<float, DOMINANT_HUE_BINS> hueChromaSums{};
	std::array<float, DOMINANT_HUE_BINS> hueLightnessSums{};
	std::array<float, DOMINANT_HUE_BINS> hueCosSums{};
	std::array<float, DOMINANT_HUE_BINS> hueSinSums{};
	std::array<float3, DOMINANT_HUE_BINS> hueLinearSums{};
	float totalHueWeight = 0.0f;
	float sceneLightnessSum = 0.0f;
	float sceneLightnessWeight = 0.0f;
	float3 ambientSum(0, 0, 0);
	float ambientWeight = 0.0f;
	size_t consideredPixelCount = 0;
	size_t neutralScenePixelCount = 0;
	size_t coloredScenePixelCount = 0;

	for (const uint32_t colorOffset : colors)
	{
		const byte3 nonlinear(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]);
		const float3 nonlinear01 = static_cast<float3>(nonlinear) / 255.0f;
		const float maxComponent = linalg::maxelem(nonlinear01);
		const float minComponent = linalg::minelem(nonlinear01);
		const float rgbChroma = maxComponent - minComponent;
		const float saturation = (maxComponent > 0.0001f) ? rgbChroma / maxComponent : 0.0f;
		const float luma = 0.2126f * nonlinear01.x + 0.7152f * nonlinear01.y + 0.0722f * nonlinear01.z;

		if (suppressSparseBrightNeutralHighlights && isBrightNeutralHighlight(_dominantColorConfig, maxComponent, saturation, luma))
		{
			continue;
		}

		consideredPixelCount++;

		const float visible = std::clamp((luma - 0.025f) / 0.18f, 0.0f, 1.0f);
		const float3 linear = static_cast<float3>(InfiniteProcessing::srgbNonlinearToLinear(nonlinear)) / 65535.0f;
		const float3 oklab = ColorSpaceMath::linear_rgb_to_oklab(linear);
		const float oklabChroma = std::sqrt(oklab.y * oklab.y + oklab.z * oklab.z);
		const float sceneWeight = 0.15f + 0.85f * visible;

		if (saturation <= _dominantColorConfig.oklchNeutralSceneMaxSaturation && luma >= _dominantColorConfig.oklchNeutralSceneMinLuma)
		{
			neutralScenePixelCount++;
		}
		else if (oklabChroma > OKLCH_MIN_CHROMA && saturation > _dominantColorConfig.oklchNeutralSceneMaxSaturation && visible > 0.0f)
		{
			coloredScenePixelCount++;
		}

		ambientSum += linear;
		ambientWeight += 1.0f;
		sceneLightnessSum += oklab.x * sceneWeight;
		sceneLightnessWeight += sceneWeight;

		if (oklabChroma > OKLCH_MIN_CHROMA && saturation > 0.05f && visible > 0.0f)
		{
			const float hue = std::atan2(oklab.z, oklab.y);
			const float chromaConfidence = std::clamp(oklabChroma / OKLCH_CHROMA_REFERENCE, 0.0f, 1.0f);
			const float hueWeight = visible * maxComponent * chromaConfidence * chromaConfidence * (0.25f + 0.75f * saturation);
			const int hueBin = oklchHueToBin(hue);

			hueWeights[hueBin] += hueWeight;
			hueChromaSums[hueBin] += oklabChroma * hueWeight;
			hueLightnessSums[hueBin] += oklab.x * hueWeight;
			hueCosSums[hueBin] += std::cos(hue) * hueWeight;
			hueSinSums[hueBin] += std::sin(hue) * hueWeight;
			hueLinearSums[hueBin] += linear * hueWeight;
			totalHueWeight += hueWeight;
		}
	}

	const float neutralSceneCoverage = (consideredPixelCount > 0) ? static_cast<float>(neutralScenePixelCount) / static_cast<float>(consideredPixelCount) : 0.0f;
	const float coloredSceneCoverage = (consideredPixelCount > 0) ? static_cast<float>(coloredScenePixelCount) / static_cast<float>(consideredPixelCount) : 0.0f;
	const bool hasEnoughColoredCoverage = coloredSceneCoverage >= _dominantColorConfig.oklchMinColoredCoverage;
	const bool protectNeutralScene =
		_dominantColorConfig.oklchNeutralSceneProtection &&
		neutralSceneCoverage >= _dominantColorConfig.oklchNeutralSceneMinCoverage &&
		!hasEnoughColoredCoverage;

	if (protectNeutralScene && ambientWeight > 0.0001f)
	{
		return ambientSum / ambientWeight;
	}

	float bestHueWeight = 0.0f;
	float bestHueChromaSum = 0.0f;
	float bestHueLightnessSum = 0.0f;
	float bestHueCosSum = 0.0f;
	float bestHueSinSum = 0.0f;
	float3 bestHueLinearSum(0, 0, 0);

	for (int i = 0; i < DOMINANT_HUE_BINS; ++i)
	{
		const int prev = (i + DOMINANT_HUE_BINS - 1) % DOMINANT_HUE_BINS;
		const int next = (i + 1) % DOMINANT_HUE_BINS;
		const float candidateWeight = hueWeights[prev] + hueWeights[i] + hueWeights[next];

		if (candidateWeight > bestHueWeight)
		{
			bestHueWeight = candidateWeight;
			bestHueChromaSum = hueChromaSums[prev] + hueChromaSums[i] + hueChromaSums[next];
			bestHueLightnessSum = hueLightnessSums[prev] + hueLightnessSums[i] + hueLightnessSums[next];
			bestHueCosSum = hueCosSums[prev] + hueCosSums[i] + hueCosSums[next];
			bestHueSinSum = hueSinSums[prev] + hueSinSums[i] + hueSinSums[next];
			bestHueLinearSum = hueLinearSums[prev] + hueLinearSums[i] + hueLinearSums[next];
		}
	}

	const float dominantShare = (totalHueWeight > 0.0001f) ? bestHueWeight / totalHueWeight : 0.0f;
	const float hueCoherence = (bestHueWeight > 0.0001f) ? std::sqrt(bestHueCosSum * bestHueCosSum + bestHueSinSum * bestHueSinSum) / bestHueWeight : 0.0f;
	const bool hasClearDominantHue =
		bestHueWeight > 0.0001f &&
		dominantShare >= OKLCH_MIN_DOMINANT_SHARE &&
		hueCoherence >= OKLCH_MIN_HUE_COHERENCE &&
		hasEnoughColoredCoverage;

	if (hasClearDominantHue)
	{
		const float dominantHue = std::atan2(bestHueSinSum, bestHueCosSum);
		const float sceneLightness = (sceneLightnessWeight > 0.0001f) ? sceneLightnessSum / sceneLightnessWeight : bestHueLightnessSum / bestHueWeight;
		const float dominantLightness = bestHueLightnessSum / bestHueWeight;
		const float dominantBrightnessBlend = std::clamp(_dominantColorConfig.oklchDominantBrightnessBlend, 0.0f, 1.0f);
		const float targetLightness = sceneLightness * (1.0f - dominantBrightnessBlend) + dominantLightness * dominantBrightnessBlend;
		const float dominantChroma = bestHueChromaSum / bestHueWeight;
		const float targetChroma = std::clamp(dominantChroma * OKLCH_COLORFULNESS_BOOST, 0.0f, 0.34f);
		const float3 oklab{
			std::clamp(targetLightness, 0.0f, 1.0f),
			std::cos(dominantHue) * targetChroma,
			std::sin(dominantHue) * targetChroma
		};
		const float3 sourceAverage = clampFloat3(bestHueLinearSum / bestHueWeight, 0.0f, 1.0f);
		const float3 sourceOklab = ColorSpaceMath::linear_rgb_to_oklab(sourceAverage);
		const float3 reconstructed = clampFloat3(ColorSpaceMath::oklab_to_linear_rgb(ColorSpaceMath::clamp_oklab_chroma_to_gamut(oklab)), 0.0f, 1.0f);

		// Keep perceptual OKLCH reconstruction for midtones, but preserve source RGB ratios for bright or deep saturated colors.
		const float darkColorPreserve = 1.0f - smoothStep(0.32f, 0.48f, dominantLightness);
		const float brightColorPreserve = smoothStep(0.58f, 0.76f, dominantLightness);
		const float saturatedColorConfidence = smoothStep(0.04f, 0.12f, dominantChroma);
		const float sourcePreserve = std::clamp(std::max(darkColorPreserve, brightColorPreserve) * saturatedColorConfidence, 0.0f, 0.85f);
		const float lightnessRatio = (sourceOklab.x > 0.0001f) ? std::clamp(targetLightness / sourceOklab.x, 0.0f, 2.0f) : 0.0f;
		const float sourceScale = lightnessRatio * lightnessRatio * lightnessRatio;
		const float3 sourceAtTargetLightness = clampFloat3(sourceAverage * sourceScale, 0.0f, 1.0f);

		return reconstructed * (1.0f - sourcePreserve) + sourceAtTargetLightness * sourcePreserve;
	}

	if (ambientWeight > 0.0001f)
	{
		return ambientSum / ambientWeight;
	}

	return calcVividMulticolorForLeds(image, colors);
}

float3 ImageColorAveraging::calcUnicolorForLeds(const Image<ColorRgb>& image) const
{
	uint_fast64_t sum = 0;
	linalg::vec<uint_fast64_t, 3> sumLinear(0, 0, 0);

	const uint8_t* imgData = image.rawMem();
	const uint32_t rowSize = image.width() * static_cast<uint32_t>(sizeof(ColorRgb));
	const uint32_t increment = (_sparseProcessing) ? 2 : 1;

	for (uint32_t y = 0; y < image.height(); y += increment)
	{
		for (uint32_t colorOffset = y * rowSize; colorOffset < y * rowSize + rowSize; colorOffset += increment * static_cast<uint32_t>(sizeof(ColorRgb)))
		{
			sumLinear += InfiniteProcessing::srgbNonlinearToLinear(byte3(imgData[colorOffset], imgData[colorOffset + 1], imgData[colorOffset + 2]));
			sum++;
		}
	}

	auto averageLinear = (static_cast<float3>(sumLinear) / static_cast<float>(sum)) / 65535.0f;

	return averageLinear;
}

