/* ImageToLedManager.cpp
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

#include <base/HyperHdrInstance.h>
#include <base/ImageToLedManager.h>
#include <base/ImageColorAveraging.h>
#include <blackborder/BlackBorderProcessor.h>
#include <infinite-color-engine/ColorSpace.h>
#include <utils/InternalClock.h>

#include <algorithm>
#include <cmath>

using namespace hyperhdr;
using namespace linalg::aliases;

void ImageToLedManager::registerProcessingUnit(
	const unsigned width,
	const unsigned height,
	const unsigned horizontalBorder,
	const unsigned verticalBorder)
{
	if (width > 0 && height > 0)
	{
		_colorAveraging = std::make_unique<ImageColorAveraging>(
			_log,
			_mappingType,
			_sparseProcessing,
			width,
			height,
			horizontalBorder,
			verticalBorder,
			_instanceIndex,
			_ledString.leds());
		_colorAveraging->setAmbientChromaMax(_ambientChromaMax);
	}
	else
		_colorAveraging = nullptr;

	// The averaging object (and therefore the LED geometry/count) just changed; drop any temporal
	// ambient state so it re-seeds cleanly on the next frame.
	resetAmbientState();
}


// global transform method
int ImageToLedManager::mappingTypeToInt(const QString& mappingType)
{
	if (mappingType == "advanced_ambient")
		return 4;

	if (mappingType == "advanced_dominant")
		return 3;

	if (mappingType == "advanced_vivid")
		return 2;

	if (mappingType == "unicolor_mean")
		return 1;

	return 0;
}
// global transform method
QString ImageToLedManager::mappingTypeToStr(int mappingType)
{
	if (mappingType == 4)
		return "advanced_ambient";

	if (mappingType == 3)
		return "advanced_dominant";

	if (mappingType == 2)
		return "advanced_vivid";

	if (mappingType == 1)
		return "unicolor_mean";

	return "advanced";
}

ImageToLedManager::ImageToLedManager(const LedString& ledString, HyperHdrInstance* hyperhdr)
	: QObject(hyperhdr)
	, _instanceIndex(hyperhdr->getInstanceIndex())
	, _log(QString("IMAGETOLED_MNG%1").arg(_instanceIndex))
	, _ledString(ledString)
	, _borderProcessor(new BlackBorderProcessor(hyperhdr, this))
	, _colorAveraging(nullptr)
	, _mappingType(0)
	, _sparseProcessing(false)
{
	// init
	handleSettingsUpdate(settings::type::COLOR, hyperhdr->getSetting(settings::type::COLOR));
	// listen for changes in color - ledmapping
	connect(hyperhdr, &HyperHdrInstance::SignalInstanceSettingsChanged, this, &ImageToLedManager::handleSettingsUpdate);
	connect(this, &ImageToLedManager::SignalImageToLedsMappingChanged, hyperhdr, &HyperHdrInstance::SignalImageToLedsMappingChanged);

	Debug(_log, "ImageToLedManager initialized");
}

void ImageToLedManager::handleSettingsUpdate(settings::type type, const QJsonDocument& config)
{
	if (type == settings::type::COLOR)
	{
		const QJsonObject& obj = config.object();
		int newType = mappingTypeToInt(obj["imageToLedMappingType"].toString());
		setLedMappingType(newType);

		bool newSparse = obj["sparse_processing"].toBool(false);
		setSparseProcessing(newSparse);

		// "advanced_ambient" knobs (all optional, with the same defaults as the schema).
		_ambientChromaMax = static_cast<float>(obj["ambientTintStrength"].toDouble(0.06));
		_ambientSettlingMs = static_cast<float>(obj["ambientSettlingMs"].toDouble(650.0));
		_ambientCutSensitivity = static_cast<float>(obj["ambientCutSensitivity"].toDouble(4.0));
		if (_colorAveraging != nullptr)
			_colorAveraging->setAmbientChromaMax(_ambientChromaMax);
	}
}

void ImageToLedManager::setSize(unsigned width, unsigned height)
{
	// Check if the existing buffer-image is already the correct dimensions
	if (_colorAveraging != nullptr && _colorAveraging->width() == width && _colorAveraging->height() == height)
	{
		return;
	}

	// Construct a new buffer and mapping
	registerProcessingUnit(width, height, 0, 0);
}

void ImageToLedManager::setLedString(const LedString& ledString)
{
	if (_colorAveraging != nullptr)
	{
		_ledString = ledString;

		// get current width/height
		unsigned width = _colorAveraging->width();
		unsigned height = _colorAveraging->height();

		// Construct a new buffer and mapping
		registerProcessingUnit(width, height, 0, 0);
	}
}

void ImageToLedManager::setBlackbarDetectDisable(bool enable)
{
	_borderProcessor->setHardDisable(enable);
}

bool ImageToLedManager::blackBorderDetectorEnabled() const
{
	return _borderProcessor->enabled();
}

void ImageToLedManager::processFrame(std::vector<float3>& ledColors, const Image<ColorRgb>& frameBuffer)
{
	setSize(frameBuffer);;
	verifyBorder(frameBuffer);

	if (_colorAveraging != nullptr && _colorAveraging->width() == frameBuffer.width() && _colorAveraging->height() == frameBuffer.height())
	{
		_colorAveraging->process(ledColors, frameBuffer);

		if (_mappingType == 4)
			applyAmbientTemporal(ledColors, frameBuffer);
	}
}

void ImageToLedManager::setSparseProcessing(bool sparseProcessing)
{
	bool _orgmappingType = _sparseProcessing;

	_sparseProcessing = sparseProcessing;

	Debug(_log, "setSparseProcessing to {:d}", _sparseProcessing);
	if (_orgmappingType != _sparseProcessing && _colorAveraging != nullptr)
	{
		unsigned width = _colorAveraging->width();
		unsigned height = _colorAveraging->height();

		registerProcessingUnit(width, height, 0, 0);
	}
}

void ImageToLedManager::setLedMappingType(int mapType)
{
	int _orgmappingType = _mappingType;

	_mappingType = mapType;

	Debug(_log, "Set LED mapping type to {:s}", (mappingTypeToStr(mapType)));

	if (_orgmappingType != _mappingType && _colorAveraging != nullptr)
	{
		unsigned width = _colorAveraging->width();
		unsigned height = _colorAveraging->height();

		registerProcessingUnit(width, height, 0, 0);
	}

	if (_orgmappingType != _mappingType)
	{
		emit SignalImageToLedsMappingChanged(_mappingType);
	}
}

bool ImageToLedManager::getScanParameters(size_t led, double& hscanBegin, double& hscanEnd, double& vscanBegin, double& vscanEnd) const
{
	if (led < _ledString.leds().size())
	{
		const LedString::Led& l = _ledString.leds()[led];
		hscanBegin = l.minX_frac;
		hscanEnd = l.maxX_frac;
		vscanBegin = l.minY_frac;
		vscanEnd = l.maxY_frac;
	}

	return false;
}

void ImageToLedManager::verifyBorder(const Image<ColorRgb>& image)
{
	if (!_borderProcessor->enabled() && (_colorAveraging->horizontalBorder() != 0 || _colorAveraging->verticalBorder() != 0))
	{
		Debug(_log, "Reset border");
		_borderProcessor->process(image);

		registerProcessingUnit(image.width(), image.height(), 0, 0);
	}

	if (_borderProcessor->enabled() && _borderProcessor->process(image))
	{
		const hyperhdr::BlackBorder border = _borderProcessor->getCurrentBorder();

		if (border.unknown)
		{
			// Construct a new buffer and mapping
			registerProcessingUnit(image.width(), image.height(), 0, 0);
		}
		else
		{
			// Construct a new buffer and mapping
			registerProcessingUnit(image.width(), image.height(), border.horizontalSize, border.verticalSize);
		}
	}
}

void ImageToLedManager::setSize(const Image<ColorRgb>& image)
{
	setSize(image.width(), image.height());
}

int ImageToLedManager::getLedMappingType() const
{
	return _mappingType;
}

void ImageToLedManager::resetAmbientState()
{
	_ambientStateValid = false;
	_ambientHistValid = false;
	_ambientRecentCount = 0;
	_ambientRecentPos = 0;
	_ambientCutPending = false;
	_ambientCutLockoutUntil = 0;
	_ambientLastTs = 0;
	_ambientStateOklab.clear();
}

float ImageToLedManager::ambientSceneCutDistance(const Image<ColorRgb>& frameBuffer)
{
	// Coarse 8x8x8 RGB histogram (sparse sampling) compared to the previous frame via L1 distance.
	// This is an INDEPENDENT signal (not derived from the smoothed LED estimate), so a slow pan cannot
	// trip a false cut the way a "raw-vs-smoothed-state" metric would.
	const unsigned w = frameBuffer.width();
	const unsigned h = frameBuffer.height();
	if (w == 0 || h == 0)
		return 0.0f;

	std::array<float, 512> hist{};
	const uint8_t* imgData = frameBuffer.rawMem();
	constexpr unsigned step = 16;	// pixel stride
	unsigned long long count = 0;

	for (unsigned y = 0; y < h; y += step)
	{
		const uint8_t* row = imgData + static_cast<size_t>(y) * w * 3;
		for (unsigned x = 0; x < w; x += step)
		{
			const uint8_t* p = row + static_cast<size_t>(x) * 3;
			const int bin = ((p[0] >> 5) << 6) | ((p[1] >> 5) << 3) | (p[2] >> 5);
			hist[bin] += 1.0f;
			count++;
		}
	}

	if (count == 0)
		return 0.0f;

	const float inv = 1.0f / static_cast<float>(count);
	float distance = 0.0f;
	if (_ambientHistValid)
	{
		for (int i = 0; i < 512; ++i)
			distance += std::fabs(hist[i] * inv - _ambientPrevHist[i]);
	}
	for (int i = 0; i < 512; ++i)
		_ambientPrevHist[i] = hist[i] * inv;
	_ambientHistValid = true;

	return distance;	// range 0..2 (L1 over two normalized distributions)
}

void ImageToLedManager::applyAmbientTemporal(std::vector<float3>& ledColors, const Image<ColorRgb>& frameBuffer)
{
	const size_t count = ledColors.size();
	if (count == 0)
		return;

	const long long now = InternalClock::nowPrecise();
	const float dist = ambientSceneCutDistance(frameBuffer);

	// --- scene-cut detection: a spike confirmed by a following calm frame ---
	// A hard cut is a single large distance, then the new shot is stable. A flash (lightning) is two
	// large distances (in and out), so deferring the snap by one calm frame rejects flashes while still
	// snapping cuts ~1 frame late (imperceptible). The slow EMA already absorbs 1-frame flashes anyway.
	bool sceneCut = false;
	float threshold = 1.0f;
	if (_ambientRecentCount >= 10)
	{
		const int n = std::min(_ambientRecentCount, static_cast<int>(_ambientRecentDist.size()));
		std::array<float, 32> tmp{};
		for (int i = 0; i < n; ++i)
			tmp[i] = _ambientRecentDist[i];
		std::nth_element(tmp.begin(), tmp.begin() + n / 2, tmp.begin() + n);
		const float median = tmp[n / 2];
		threshold = std::max(0.02f, median * _ambientCutSensitivity);

		if (now >= _ambientCutLockoutUntil)
		{
			if (_ambientCutPending)
			{
				if (dist <= threshold)
				{
					// spike followed by a calm frame => a real cut on stable new content
					sceneCut = true;
					_ambientCutLockoutUntil = now + 200;	// ms anti-strobe lockout
				}
				_ambientCutPending = false;	// either confirmed, or it was a flash/transition
			}
			else if (dist > threshold)
			{
				_ambientCutPending = true;	// candidate; confirm next frame
			}
		}
	}

	// Record this frame's distance into the ring buffer (baseline for the median).
	_ambientRecentDist[_ambientRecentPos] = dist;
	_ambientRecentPos = (_ambientRecentPos + 1) % static_cast<int>(_ambientRecentDist.size());
	if (_ambientRecentCount < static_cast<int>(_ambientRecentDist.size()))
		_ambientRecentCount++;

	// --- (re)seed temporal state on first frame / geometry change ---
	if (!_ambientStateValid || _ambientStateOklab.size() != count)
	{
		_ambientStateOklab.resize(count);
		for (size_t i = 0; i < count; ++i)
			_ambientStateOklab[i] = ColorSpaceMath::linear_rgb_to_oklab(ledColors[i]);
		_ambientStateValid = true;
		_ambientLastTs = now;
		return;	// emit the raw estimate as-is on the seed frame
	}

	// --- dt-correct EMA in OKLab; snap instantly on a confirmed cut ---
	float alpha;
	if (sceneCut)
	{
		alpha = 1.0f;
	}
	else
	{
		long long dtMs = now - _ambientLastTs;
		dtMs = std::clamp<long long>(dtMs, 1, 250);
		const float tau = std::max(1.0f, _ambientSettlingMs);
		alpha = 1.0f - std::exp(-static_cast<float>(dtMs) / tau);
	}
	_ambientLastTs = now;

	for (size_t i = 0; i < count; ++i)
	{
		const float3 raw = ColorSpaceMath::linear_rgb_to_oklab(ledColors[i]);
		float3& state = _ambientStateOklab[i];
		state = state + (raw - state) * alpha;
		ledColors[i] = linalg::clamp(ColorSpaceMath::oklab_to_linear_rgb(state), 0.0f, 1.0f);
	}
}
