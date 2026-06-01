#pragma once

#ifndef PCH_ENABLED
	#include <QString>

	#include <array>
	#include <memory>
	#include <vector>
#endif

#include <image/Image.h>
#include <utils/Logger.h>
#include <base/LedString.h>
#include <base/ImageColorAveraging.h>
#include <blackborder/BlackBorderProcessor.h>

#include <linalg.h>

class HyperHdrInstance;

class ImageToLedManager : public QObject
{
	Q_OBJECT

public:
	ImageToLedManager(const LedString& ledString, HyperHdrInstance* hyperhdr);

	void setSize(unsigned width, unsigned height);
	void setLedString(const LedString& ledString);
	bool blackBorderDetectorEnabled() const;
	int getLedMappingType() const;

	static int mappingTypeToInt(const QString& mappingType);
	static QString mappingTypeToStr(int mappingType);

	void setSparseProcessing(bool sparseProcessing);
	void processFrame(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& frameBuffer);

signals:
	void SignalImageToLedsMappingChanged(int mappingType);

public slots:
	void setBlackbarDetectDisable(bool enable);
	void setLedMappingType(int mapType);

public:
	void setSize(const Image<ColorRgb>& image);
	void verifyBorder(const Image<ColorRgb>& image);
	bool getScanParameters(size_t led, double& hscanBegin, double& hscanEnd, double& vscanBegin, double& vscanEnd) const;

private:
	void registerProcessingUnit(
		const unsigned width,
		const unsigned height,
		const unsigned horizontalBorder,
		const unsigned verticalBorder);

	// "advanced_ambient" estimate-level temporal stabilization (Stage 2). Lives here because the
	// ImageColorAveraging object is rebuilt on every geometry/border/mode change, while this manager
	// persists for the lifetime of the instance. Only active when _mappingType == 4.
	void applyAmbientTemporal(std::vector<linalg::aliases::float3>& ledColors, const Image<ColorRgb>& frameBuffer);
	void resetAmbientState();
	float ambientSceneCutDistance(const Image<ColorRgb>& frameBuffer);

private slots:
	void handleSettingsUpdate(settings::type type, const QJsonDocument& config);

private:
	quint8		_instanceIndex;
	LoggerName	_log;
	LedString	_ledString;
	hyperhdr::BlackBorderProcessor* _borderProcessor;
	std::unique_ptr<hyperhdr::ImageColorAveraging> _colorAveraging;
	int		_mappingType;
	bool	_sparseProcessing;

	// Ambient-mode settings.
	float	_ambientChromaMax = 0.06f;
	float	_ambientSettlingMs = 650.0f;
	float	_ambientCutSensitivity = 4.0f;

	// Ambient-mode per-frame temporal state.
	std::vector<linalg::aliases::float3> _ambientStateOklab;
	bool		_ambientStateValid = false;
	long long	_ambientLastTs = 0;
	std::array<float, 512> _ambientPrevHist{};	// coarse 8x8x8 RGB histogram of the previous frame
	bool		_ambientHistValid = false;
	std::array<float, 32> _ambientRecentDist{};	// ring buffer of recent frame-to-frame histogram distances
	int			_ambientRecentCount = 0;
	int			_ambientRecentPos = 0;
	bool		_ambientCutPending = false;	// a spike was seen; confirm as a cut only if the next frame is calm
	long long	_ambientCutLockoutUntil = 0;
};
