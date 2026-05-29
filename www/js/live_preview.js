$(document).ready(function() {

	var dialog;
	var modalOpened = false;
	var ledsim_width = 550;
	var ledsim_height = 489;

	var canvas_height;
	var canvas_width;
	var imageCanvasNodeCtx;
	var ledsCanvasNodeCtx;

	var leds;
	var lC = false;
	var twoDPaths = [];
	var lastFeedFrame = Date.now();
	var toggleLedsNum = false;
	var toggleColorDebug = false;
	var lastLedColors = null;
	var colorDebugLayoutMode = "normal";

	CanvasRenderingContext2D.prototype.clear = function(){
		this.clearRect(0, 0, this.canvas.width, this.canvas.height)
	};

	function create2dPaths(){
		twoDPaths = [];
		for(var idx=0; idx<leds.length; idx++)
		{
			var led = leds[idx];
			twoDPaths.push( build2DPath(led.hmin * canvas_width, led.vmin * canvas_height, (led.hmax-led.hmin) * canvas_width, (led.vmax-led.vmin) * canvas_height, 5) );
		}
	};
	
	function build2DPath(x, y, width, height, radius) {

		if (typeof radius == 'number')
		{
			radius = {tl: radius, tr: radius, br: radius, bl: radius};
		}
		else
		{
			var defaultRadius = {tl: 0, tr: 0, br: 0, bl: 0};
			for (var side in defaultRadius)
			{
				radius[side] = radius[side] || defaultRadius[side];
			}
		}

		var path = new Path2D();

		path.moveTo(x + radius.tl, y);
		path.lineTo(x + width - radius.tr, y);
		path.quadraticCurveTo(x + width, y, x + width, y + radius.tr);
		path.lineTo(x + width, y + height - radius.br);
		path.quadraticCurveTo(x + width, y + height, x + width - radius.br, y + height);
		path.lineTo(x + radius.bl, y + height);
		path.quadraticCurveTo(x, y + height, x, y + height - radius.bl);
		path.lineTo(x, y + radius.tl);
		path.quadraticCurveTo(x, y, x + radius.tl, y);

		return path;
	};

	function takeCareButton(force = false)
	{
		if($("#live_preview_dialog").outerWidth() < 550 || force)
		{
			$('#vid_btn_1').text($.i18n('main_ledsim_btn_toggleleds').substring(0,4));
			$('#vid_btn_2').text($.i18n('main_ledsim_btn_togglelednumber').substring(0,4));
			$('#vid_btn_3').text($.i18n('main_ledsim_btn_togglelivevideo').substring(0,4));
			$('#vid_btn_4').text($.i18n('main_ledsim_btn_screenshot').substring(0,4));
			$('#vid_btn_5').text($.i18n('main_ledsim_btn_toggledebug').substring(0,4));
		}
		else
		{			
			$('#vid_btn_1').text($.i18n('main_ledsim_btn_toggleleds'));
			$('#vid_btn_2').text($.i18n('main_ledsim_btn_togglelednumber'));
			$('#vid_btn_3').text($.i18n('main_ledsim_btn_togglelivevideo'));
			$('#vid_btn_4').text($.i18n('main_ledsim_btn_screenshot'));
			$('#vid_btn_5').text($.i18n('main_ledsim_btn_toggledebug'));
		}
	};

	function getReady()
	{
		leds = window.serverConfig.leds;

		if(getStorage('ledsim_width') != null)
		{
			ledsim_width = getStorage('ledsim_width');
			ledsim_height = getStorage('ledsim_height');
		}

		dialog = $("#live_preview_dialog").dialog({
			uiLibrary: 'bootstrap5',
			resizable: true,
			modal: false,
			minWidth: 330,
			width: ledsim_width,
			minHeight: 320,
			height: ledsim_height,
			closeOnEscape: true,
			autoOpen: false,
			title: '<svg data-src="svg/live_preview.svg" fill="currentColor" class="svg4hyperhdr"></svg>' + $.i18n('main_ledsim_title'),
			resize: function (e) {
				updateLedLayout();
			},
			opened: function (e) {
				if(!lC)
				{
					updateLedLayout();
					lC = true;
				}
				modalOpened = true;

				if($('#leds_toggle').hasClass('btn-success'))
					requestLedColorsStart();

				if($('#leds_toggle_live_video').hasClass('btn-success'))
					requestLedImageStart();
			},
			closed: function (e) {
				modalOpened = false;
				if (!toggleColorDebug)
					requestLedColorsStop();
				requestLedImageStop();
			},
			resizeStop: function (e) {
				setStorage("ledsim_width", $("#live_preview_dialog").outerWidth());
				setStorage("ledsim_height", $("#live_preview_dialog").outerHeight());
				
				takeCareButton();
				updateLedLayout();
			}
		});

		var ua = window.navigator.userAgent;
		var iOS = !!ua.match(/iPad/i) || !!ua.match(/iPhone/i);
		var webkitDefined = !!ua.match(/WebKit/i);
		var iOSSafari = iOS && webkitDefined && !ua.match(/CriOS/i);

		if (navigator.maxTouchPoints > 0 || iOSSafari)
		{
			var btnn = $("#live_preview_dialog").find("button.btn-close");
			if (btnn.length == 1 && !$(btnn[0]).parent().is($("#live_preview_dialog")))
			{
				$(btnn[0]).css('position','absolute');
				$(btnn[0]).css('right','10px');
				$(btnn[0]).css('top','20px');
				$("#live_preview_dialog").append( $(btnn[0]) );
			}
		}
		
		$(window.hyperhdr).on("cmd-config-getconfig",function(event){
			leds = event.response.info.leds;
			updateLedLayout();
		});

		$(window.hyperhdr).on("cmd-instance-switchTo", function (event) {
			setTimeout(function(){
				if($('#live_preview_dialog').is(':visible'))
				{
					if ( $('#leds_toggle_live_video').hasClass("btn-success"))
					{
						requestLedImageStart();					
					}
					if ( $('#leds_toggle').hasClass("btn-success"))
					{
						requestLedColorsStart();					
					}
				}
				if (toggleColorDebug)
				{
					requestLedColorsStart();
				}
			}, 500);
		});
	};

	function resetImage(){
		imageCanvasNodeCtx.fillStyle = "rgb(0,0,0)"
		imageCanvasNodeCtx.fillRect(0, 0, canvas_width, canvas_height);

		var image = new Image();
		var sourceImg = document.getElementById("left_top_hyperhdr_logo");
		if (sourceImg.naturalWidth > 0 && sourceImg.naturalHeight > 0)
		{
			var x = Math.max(canvas_width/2 - 130, 0);
			var y = Math.max(canvas_height/2 - 38, 0);
			imageCanvasNodeCtx.drawImage(sourceImg, x, y, 262, 83);

			imageCanvasNodeCtx.font = "16px monospace";
			imageCanvasNodeCtx.textAlign = "center";
			imageCanvasNodeCtx.fillStyle = "white";

			if (!window.imageStreamActive)
			{
				imageCanvasNodeCtx.fillText($.i18n('preview_live_video_is_paused'), x + 131, y + 100);
			}
			else
			{
				imageCanvasNodeCtx.fillText($.i18n('preview_no_signal'), x + 131, y + 100);
			}
		}
	};	
	
	$(window.hyperhdr).one("ready",function(){
		getReady();		
	});

	function printLedsToCanvas(colors)
	{
		if(!window.ledStreamActive)
			return;

		var useColor = false;
		var cPos = 0;
		ledsCanvasNodeCtx.clear();

		if(typeof colors != "undefined")
			useColor = true;

		if(colors && colors.length/3 < leds.length)
			return;

		for(var idx=0; idx<leds.length; idx++)
		{
			var led = leds[idx];

			ledsCanvasNodeCtx.fillStyle = (useColor) ?  "rgba("+colors[cPos]+","+colors[cPos+1]+","+colors[cPos+2]+",0.75)"  : "hsla("+(idx*360/leds.length)+",100%,50%,0.75)";
			ledsCanvasNodeCtx.fill(twoDPaths[idx]);
			ledsCanvasNodeCtx.stroke(twoDPaths[idx]);

			if(toggleLedsNum)
			{
				ledsCanvasNodeCtx.fillStyle = "white";
				ledsCanvasNodeCtx.textAlign = "center";
				ledsCanvasNodeCtx.fillText(((led.name) ? led.name : idx), (led.hmin * canvas_width) + ( ((led.hmax-led.hmin) * canvas_width) / 2), 3 + (led.vmin * canvas_height) + ( ((led.vmax-led.vmin) * canvas_height) / 2));
			}

			cPos += 3;
		}

		updateColorDebug(colors);
	};

	function averageColor(colors, firstLed, lastLed)
	{
		var red = 0;
		var green = 0;
		var blue = 0;
		var count = 0;

		for (var led = firstLed; led < lastLed; led++)
		{
			var pos = led * 3;
			red += colors[pos];
			green += colors[pos + 1];
			blue += colors[pos + 2];
			count++;
		}

		return (count > 0) ? [
			Math.round(red / count),
			Math.round(green / count),
			Math.round(blue / count)
		] : [0, 0, 0];
	};

	function colorDebugRow(label, rgb)
	{
		return '<div class="led-color-debug-row">' +
			colorDebugSwatch('led-color-debug-row-swatch', rgb) +
			'<span class="led-color-debug-row-label">' + label + '</span>' +
			'<code class="led-color-debug-row-rgb">R ' + rgb[0] + ' / G ' + rgb[1] + ' / B ' + rgb[2] + '</code>' +
			'<code class="led-color-debug-row-hex">' + rgbHex(rgb) + '</code>' +
			'</div>';
	};

	function clampColorChannel(value)
	{
		return Math.max(0, Math.min(255, Math.round(Number(value) || 0)));
	};

	function rgbCss(rgb)
	{
		return 'rgb(' + clampColorChannel(rgb[0]) + ',' + clampColorChannel(rgb[1]) + ',' + clampColorChannel(rgb[2]) + ')';
	};

	function rgbHex(rgb)
	{
		return '#' + rgb.map(function(channel) {
			return clampColorChannel(channel).toString(16).padStart(2, '0');
		}).join('').toUpperCase();
	};

	function colorDebugSwatch(className, rgb, title)
	{
		var normalized = [
			clampColorChannel(rgb[0]),
			clampColorChannel(rgb[1]),
			clampColorChannel(rgb[2])
		];
		var safeTitle = title || ('R ' + normalized[0] + ' / G ' + normalized[1] + ' / B ' + normalized[2]);

		return '<canvas class="' + className + ' led-color-debug-canvas-swatch darkreader-ignore" width="48" height="48" ' +
			'data-r="' + normalized[0] + '" data-g="' + normalized[1] + '" data-b="' + normalized[2] + '" ' +
			'title="' + safeTitle + '" style="background-color:' + rgbCss(normalized) + ' !important;"></canvas>';
	};

	function drawColorDebugSwatches()
	{
		$('.led-color-debug-canvas-swatch').each(function() {
			var rgb = [
				clampColorChannel($(this).attr('data-r')),
				clampColorChannel($(this).attr('data-g')),
				clampColorChannel($(this).attr('data-b'))
			];
			var context = this.getContext('2d');

			if (!context)
				return;

			context.clearRect(0, 0, this.width, this.height);
			context.fillStyle = rgbCss(rgb);
			context.fillRect(0, 0, this.width, this.height);
		});
	};

	function colorDebugLedStrip(colors)
	{
		var ledCount = Math.floor(colors.length / 3);
		var html = '<div class="led-color-debug-strip">';
		for (var led = 0; led < ledCount; led++)
		{
			var pos = led * 3;
			var rgb = [colors[pos], colors[pos + 1], colors[pos + 2]];
			html += colorDebugSwatch('led-color-debug-strip-led', rgb, 'LED ' + led + ' - R ' + rgb[0] + ' / G ' + rgb[1] + ' / B ' + rgb[2]);
		}
		html += '</div>';
		return html;
	};

	function getColorDebugPointer(event)
	{
		var originalEvent = event.originalEvent || event;
		var point = (originalEvent.touches && originalEvent.touches.length > 0) ? originalEvent.touches[0] : originalEvent;
		return { x: point.clientX, y: point.clientY };
	};

	function containColorDebugPopup()
	{
		if (colorDebugLayoutMode != "normal")
			return;

		var popup = $('#leds_color_debug_popup');
		if (!popup.length || !popup.is(':visible'))
			return;

		var left = parseFloat(popup.css('left'));
		var top = parseFloat(popup.css('top'));
		var maxLeft = Math.max(8, window.innerWidth - popup.outerWidth() - 8);
		var maxTop = Math.max(8, window.innerHeight - popup.outerHeight() - 8);

		left = Math.max(8, Math.min(isNaN(left) ? maxLeft : left, maxLeft));
		top = Math.max(8, Math.min(isNaN(top) ? maxTop : top, maxTop));
		popup.css({ left: left + 'px', top: top + 'px', right: 'auto', bottom: 'auto' });
	};

	function updateColorDebugLayoutButtons()
	{
		$('.led-color-debug-window-btn').removeClass('active');
		$('#leds_color_debug_' + colorDebugLayoutMode).addClass('active');
	};

	function setColorDebugLayout(mode)
	{
		var popup = $('#leds_color_debug_popup');
		if (!popup.length)
			return;

		colorDebugLayoutMode = mode;
		setStorage('led_color_debug_layout', mode);

		popup.removeClass('led-color-debug-full led-color-debug-left led-color-debug-right');

		if (mode == "full")
		{
			popup.addClass('led-color-debug-full');
			popup.css({ left: '0', top: '0', right: 'auto', bottom: 'auto' });
		}
		else if (mode == "left")
		{
			popup.addClass('led-color-debug-left');
			popup.css({ left: '0', top: '0', right: 'auto', bottom: 'auto' });
		}
		else if (mode == "right")
		{
			popup.addClass('led-color-debug-right');
			popup.css({ left: '50vw', top: '0', right: 'auto', bottom: 'auto' });
		}
		else
		{
			colorDebugLayoutMode = "normal";
			var storedLeft = parseFloat(getStorage('led_color_debug_left'));
			var storedTop = parseFloat(getStorage('led_color_debug_top'));
			var left = isNaN(storedLeft) ? Math.max(8, window.innerWidth - popup.outerWidth() - 18) : storedLeft;
			var top = isNaN(storedTop) ? Math.max(8, window.innerHeight - popup.outerHeight() - 18) : storedTop;
			popup.css({ left: left + 'px', top: top + 'px', right: 'auto', bottom: 'auto' });
			containColorDebugPopup();
		}

		updateColorDebugLayoutButtons();
	};

	function ensureColorDebugPopup()
	{
		if ($('#leds_color_debug_popup').length)
			return;

		var html =
			'<div id="leds_color_debug_popup" class="led-color-debug-popup" style="display:none;">' +
				'<div id="leds_color_debug_header" class="led-color-debug-header">' +
					'<div class="led-color-debug-title">' +
						'<strong>' + $.i18n('main_ledsim_debug_title') + '</strong>' +
						'<span id="leds_color_debug_meta">' + $.i18n('main_ledsim_debug_waiting') + '</span>' +
					'</div>' +
					'<div class="led-color-debug-actions">' +
						'<button type="button" id="leds_color_debug_normal" class="btn btn-sm btn-outline-light led-color-debug-window-btn" title="' + $.i18n('main_ledsim_debug_normal') + '">N</button>' +
						'<button type="button" id="leds_color_debug_left" class="btn btn-sm btn-outline-light led-color-debug-window-btn" title="' + $.i18n('main_ledsim_debug_left') + '">L</button>' +
						'<button type="button" id="leds_color_debug_right" class="btn btn-sm btn-outline-light led-color-debug-window-btn" title="' + $.i18n('main_ledsim_debug_right') + '">R</button>' +
						'<button type="button" id="leds_color_debug_full" class="btn btn-sm btn-outline-light led-color-debug-window-btn" title="' + $.i18n('main_ledsim_debug_full') + '">F</button>' +
						'<button type="button" id="leds_color_debug_close" class="btn btn-sm btn-outline-light" title="' + $.i18n('main_ledsim_debug_close') + '">X</button>' +
					'</div>' +
				'</div>' +
				'<div id="leds_color_debug_body" class="led-color-debug-body"></div>' +
			'</div>';

		$('body').append(html);

		$('#leds_color_debug_close').off().on('click', function() { closeColorDebugPopup(); });
		$('#leds_color_debug_normal').off().on('click', function() { setColorDebugLayout('normal'); });
		$('#leds_color_debug_left').off().on('click', function() { setColorDebugLayout('left'); });
		$('#leds_color_debug_right').off().on('click', function() { setColorDebugLayout('right'); });
		$('#leds_color_debug_full').off().on('click', function() { setColorDebugLayout('full'); });

		$('#leds_color_debug_header').off('mousedown.ledColorDebug touchstart.ledColorDebug').on('mousedown.ledColorDebug touchstart.ledColorDebug', function(event) {
			if ($(event.target).closest('button').length)
				return;

			if (colorDebugLayoutMode != "normal")
				setColorDebugLayout('normal');

			var popup = $('#leds_color_debug_popup');
			var pointer = getColorDebugPointer(event);
			var startLeft = parseFloat(popup.css('left'));
			var startTop = parseFloat(popup.css('top'));
			var startX = pointer.x;
			var startY = pointer.y;

			event.preventDefault();

			$(document).on('mousemove.ledColorDebugDrag touchmove.ledColorDebugDrag', function(moveEvent) {
				var movePointer = getColorDebugPointer(moveEvent);
				popup.css({
					left: (startLeft + movePointer.x - startX) + 'px',
					top: (startTop + movePointer.y - startY) + 'px',
					right: 'auto',
					bottom: 'auto'
				});
				containColorDebugPopup();
				moveEvent.preventDefault();
			});

			$(document).on('mouseup.ledColorDebugDrag touchend.ledColorDebugDrag touchcancel.ledColorDebugDrag', function() {
				$(document).off('.ledColorDebugDrag');
				setStorage('led_color_debug_left', parseFloat(popup.css('left')));
				setStorage('led_color_debug_top', parseFloat(popup.css('top')));
			});
		});

		$(window).off('resize.ledColorDebug').on('resize.ledColorDebug', function() {
			if (colorDebugLayoutMode == "normal")
				containColorDebugPopup();
		});
	};

	function openColorDebugPopup()
	{
		toggleColorDebug = true;
		ensureColorDebugPopup();
		$('#leds_color_debug_popup').show();
		setColorDebugLayout(getStorage('led_color_debug_layout') || "normal");
		setClassByBool('#leds_toggle_debug', true, "btn-success", "btn-danger");

		if (!window.ledStreamActive)
		{
			requestLedColorsStart();
			setClassByBool('#leds_toggle', false, "btn-danger", "btn-success");
		}

		updateColorDebug(lastLedColors);
	};

	function closeColorDebugPopup()
	{
		toggleColorDebug = false;
		$('#leds_color_debug_popup').hide();
		setClassByBool('#leds_toggle_debug', false, "btn-success", "btn-danger");

		if (!modalOpened && window.ledStreamActive)
			requestLedColorsStop();
	};

	function updateColorDebug(colors)
	{
		ensureColorDebugPopup();

		if (!toggleColorDebug)
		{
			$('#leds_color_debug_popup').hide();
			return;
		}

		var popup = $('#leds_color_debug_popup');
		var body = $('#leds_color_debug_body');
		var meta = $('#leds_color_debug_meta');
		popup.show();

		if (!colors || colors.length < 3)
		{
			meta.text($.i18n('main_ledsim_debug_waiting'));
			body.html('<div class="led-color-debug-waiting">' + $.i18n('main_ledsim_debug_waiting') + '</div>');
			return;
		}

		var ledCount = Math.floor(colors.length / 3);
		var average = averageColor(colors, 0, ledCount);
		var rows = '<div class="led-color-debug-summary">' +
			colorDebugSwatch('led-color-debug-main-swatch', average) +
			'<div class="led-color-debug-main-values">' +
				'<span>' + $.i18n('main_ledsim_debug_output_average') + '</span>' +
				'<strong>R ' + average[0] + ' / G ' + average[1] + ' / B ' + average[2] + '</strong>' +
				'<code>' + rgbHex(average) + '</code>' +
			'</div>' +
		'</div>';
		rows += '<div class="led-color-debug-section-title">' + $.i18n('main_ledsim_debug_samples') + '</div>';
		rows += colorDebugRow($.i18n('main_ledsim_debug_average'), average);

		if (ledCount > 1)
		{
			var middle = Math.floor(ledCount / 2);
			rows += colorDebugRow($.i18n('main_ledsim_debug_first_half'), averageColor(colors, 0, middle));
			rows += colorDebugRow($.i18n('main_ledsim_debug_second_half'), averageColor(colors, middle, ledCount));
			rows += colorDebugRow($.i18n('main_ledsim_debug_led') + ' 0', averageColor(colors, 0, 1));
			rows += colorDebugRow($.i18n('main_ledsim_debug_led') + ' ' + Math.floor(ledCount / 2), averageColor(colors, Math.floor(ledCount / 2), Math.floor(ledCount / 2) + 1));
			rows += colorDebugRow($.i18n('main_ledsim_debug_led') + ' ' + (ledCount - 1), averageColor(colors, ledCount - 1, ledCount));
		}
		else
		{
			rows += colorDebugRow($.i18n('main_ledsim_debug_led') + ' 0', averageColor(colors, 0, 1));
		}

		rows += '<div class="led-color-debug-section-title">' + $.i18n('main_ledsim_debug_led_strip') + '</div>';
		rows += colorDebugLedStrip(colors);

		meta.text(ledCount + ' ' + $.i18n('main_ledsim_debug_leds') + ' - ' + new Date().toLocaleTimeString());
		body.html(rows);
		drawColorDebugSwatches();
		containColorDebugPopup();
	};

	function leftPad(num, size) {
		num = num.toString();
		while (num.length < size) num = "0" + num;
		return num;
	};

	$('#leds_screenshot').off().on("click", function() {
		var link = document.createElement('a');
		var d = new Date();
		link.download = 'shot' + leftPad(d.getHours(),2) + leftPad(d.getMinutes(),2) + leftPad(d.getSeconds(),2) + '.png';
		link.href = document.getElementById("image_preview_canv").toDataURL()
		link.click();
		link.remove();
	});

	$('#leds_toggle').off().on("click", function() {		
		ledsCanvasNodeCtx.clear();
		setClassByBool('#leds_toggle', window.ledStreamActive, "btn-danger", "btn-success");
		if ( window.ledStreamActive )
		{
			requestLedColorsStop();
		}
		else
		{
			requestLedColorsStart();
		}
	});

	$('#leds_toggle_num').off().on("click", function() {
		toggleLedsNum = !toggleLedsNum;
		toggleClass('#leds_toggle_num', "btn-danger", "btn-success");
	});

	$('#leds_toggle_debug').off().on("click", function() {
		if (toggleColorDebug)
			closeColorDebugPopup();
		else
			openColorDebugPopup();
	});
	
	$('#leds_toggle_live_video').off().on("click", function() {
		setClassByBool('#leds_toggle_live_video', window.imageStreamActive, "btn-danger", "btn-success");
		if ( window.imageStreamActive )
		{
			lastFeedFrame = Date.now();
			requestLedImageStop();
			resetImage();
		}
		else
		{
			lastFeedFrame = Date.now();			
			requestLedImageStart();
			resetImage();
			feedWatcher();			
		}
	});

	$("#btn_open_ledsim").off().on("click", function(event) {
		
		if (dialog == null ||typeof dialog === 'undefined')
			getReady();
		
		if (window.innerWidth < 740)
		{
			$("#live_preview_dialog").width(330);
			$("#live_preview_dialog").height(320);
			
			$("#live_preview_dialog").css('top', '10px');
			$("#live_preview_dialog").css('left', '20px');			
		}
		else
		{
			var t = parseInt($('#live_preview_dialog').css('top'), 10);
			var l = parseFloat($('#live_preview_dialog').css('left'), 20);
			if ( !isNaN(t) && (t < 10 || t + 25 > window.innerHeight))
				$("#live_preview_dialog").css('top', '10px');
			if ( !isNaN(l) && (l < 20 || l + 50 > window.innerWidth))
				$("#live_preview_dialog").css('left', '20px');
		}
		$("#live_preview_dialog").css('position', 'fixed');
		
		takeCareButton();
		
		dialog.open();
		
		updateLedLayout();
	});

	function updateLedLayout()
	{
		canvas_height = $('#live_preview_dialog').outerHeight()-$('[data-role=footer]').outerHeight()-$('[data-role=header]').outerHeight()-20;
		canvas_width = $('#live_preview_dialog').outerWidth()-20;

		$('#leds_canvas').html("");
		var leds_html = '<canvas id="image_preview_canv" width="'+canvas_width+'" height="'+canvas_height+'"  style="position: absolute; left: 0; top: 0; z-index: 99998;"></canvas>';
		leds_html += '<canvas id="leds_preview_canv" width="'+canvas_width+'" height="'+canvas_height+'"  style="position: absolute; left: 0; top: 0; z-index: 99999;"></canvas>';

		$('#leds_canvas').html(leds_html);

		imageCanvasNodeCtx = document.getElementById("image_preview_canv").getContext("2d");
		ledsCanvasNodeCtx = document.getElementById("leds_preview_canv").getContext("2d");
		ledsCanvasNodeCtx.strokeStyle = "rgb(80,80,80)";
		create2dPaths();
		printLedsToCanvas();
		resetImage();
		updateColorDebug(lastLedColors);
	};


	function feedWatcher()
	{
		setTimeout(function(){
			if ( window.imageStreamActive )
			{
				var delta = Date.now() - lastFeedFrame;
				if (delta > 2000 && delta < 7000)
					resetImage();

				feedWatcher();
			};
		}, 2000);
	};


	$(window.hyperhdr).on("cmd-image-stream-frame",function(event){
		if (!modalOpened)
		{
			requestLedImageStop();
		}
		else if (window.imageStreamActive)
		{
			lastFeedFrame = Date.now();

			var imageData = (event.response);			
			var image = new Image();

			image.onload = function() {
			    imageCanvasNodeCtx.drawImage(image, 0, 0, canvas_width, canvas_height);
			};			

			var urlCreator = window.URL || window.webkitURL;
			image.src = urlCreator.createObjectURL(imageData);
		}
	});

	
	$(window.hyperhdr).on("cmd-ledcolors-ledstream-update",function(event){
		lastLedColors = event.response.result.leds;
		updateColorDebug(lastLedColors);

		if (!modalOpened && !toggleColorDebug)
		{
			requestLedColorsStop();
		}
		else if (modalOpened)
		{
			printLedsToCanvas(lastLedColors)
		}
	});

	
	$(window.hyperhdr).on("cmd-settings-update",function(event){
		var obj = event.response.data
		Object.getOwnPropertyNames(obj).forEach(function(val, idx, array) {
			window.serverInfo[val] = obj[val];
	  	});
		leds = window.serverConfig.leds
		updateLedLayout();
	});
	
});
