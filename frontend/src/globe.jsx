import React, { useEffect, useRef, useState } from "react";
import * as Cesium from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import CesiumNavigation from "cesium-navigation-es6";

Cesium.Ion.defaultAccessToken =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiIyYmMxMGJhYi04ODQ0LTQ1MWYtYjYxNC1jNDgyZGZjNTlkN2UiLCJpZCI6MzU0NDcyLCJpYXQiOjE3NjE1NzA3ODB9.ee6fK9oa_ScOtBnBnrJKMW1jZk2Zy2be8BUqwvYpIOY";

const API_URL = "http://127.0.0.1:8000/api/v1/satellites";

function Globe() {
  const cesiumContainer = useRef(null);
  const [viewer, setViewer] = useState(null);
  const [satelliteData, setSatelliteData] = useState([]);
  const hasZoomed = useRef(false);

  // 🌍 Initialize Cesium Viewer
  useEffect(() => {
    if (!cesiumContainer.current) return;

    const cesiumViewer = new Cesium.Viewer(cesiumContainer.current, {
      imageryProvider: new Cesium.IonImageryProvider({ assetId: 2 }),
      animation: false,
      timeline: false,
      fullscreenButton: true,
      geocoder: true,
      homeButton: true,
      sceneModePicker: true,
      baseLayerPicker: true,
      navigationHelpButton: true,
    });

    cesiumViewer.scene.globe.enableLighting = true;
    cesiumViewer.scene.globe.showGroundAtmosphere = true;
    cesiumViewer.scene.skyAtmosphere.show = true;

    new CesiumNavigation(cesiumViewer, {
      defaultResetView: Cesium.Cartographic.fromDegrees(0, 0, 20000000),
      enableCompass: true,
      enableZoomControls: true,
      enableDistanceLegend: true,
      enableCompassOuterRing: true,
    });

    cesiumViewer.scene.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(0.0, 0.0, 20000000),
    });

    setViewer(cesiumViewer);
    return () => cesiumViewer.destroy();
  }, []);

  // 🛰 Fetch Satellite Data
  useEffect(() => {
    let intervalId;
    async function fetchData() {
      try {
        const res = await fetch(API_URL);
        const data = await res.json();
        setSatelliteData(data.satellites || []);
      } catch (err) {
        console.error("Fetch error:", err);
      }
    }
    fetchData();
    intervalId = setInterval(fetchData, 30000); // 🔧 Fetch every 30s instead of 5s
    return () => clearInterval(intervalId);
  }, []);

  // 🛰 Animate Satellites Around Earth
  useEffect(() => {
    if (!viewer || satelliteData.length === 0) return;

    // Remove old satellite entities (need to collect IDs first to avoid modification during iteration)
    const entitiesToRemove = viewer.entities.values
      .filter(entity => {
        const id = entity.id;
        return typeof id === "string" && !id.endsWith("_orbit");
      });
    
    entitiesToRemove.forEach(entity => viewer.entities.remove(entity));

    const now = Cesium.JulianDate.now();
    
    // 🔧 Set clock to loop through the time range covered by samples
    viewer.clock.currentTime = now.clone();
    viewer.clock.clockRange = Cesium.ClockRange.LOOP_STOP;
    viewer.clock.multiplier = 1; // Real-time playback
    viewer.clock.shouldAnimate = true;

    let earliestTime = null;
    let latestTime = null;

    satelliteData.forEach((sat) => {
      if (!Array.isArray(sat.samples) || sat.samples.length < 2) return;

      const valid = sat.samples.filter(
        (s) =>
          s &&
          typeof s.lat === "number" &&
          typeof s.lon === "number" &&
          typeof s.alt_km === "number"
      );
      if (valid.length < 2) return;

      // 🧹 Remove old entities if they exist
      const orbitId = sat.norad_id + "_orbit";
      const existingSat = viewer.entities.getById(sat.norad_id);
      if (existingSat) viewer.entities.remove(existingSat);
      const existingOrbit = viewer.entities.getById(orbitId);
      if (existingOrbit) viewer.entities.remove(existingOrbit);

      // 🛰 Create orbit path
      const orbitPositions = valid.map((s) =>
        Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.alt_km * 1000)
      );

      viewer.entities.add({
        id: orbitId,
        name: sat.name + " orbit",
        polyline: {
          positions: orbitPositions,
          width: 1.2,
          material: new Cesium.PolylineGlowMaterialProperty({
            glowPower: 0.05,
            color: Cesium.Color.CYAN.withAlpha(0.25),
          }),
        },
      });

      // 🛰 Create moving satellite with time-based position
      const sampledPos = new Cesium.SampledPositionProperty();
      
      sampledPos.setInterpolationOptions({
        interpolationDegree: 5,
        interpolationAlgorithm: Cesium.LagrangePolynomialApproximation,
      });

      let startTime, endTime;
      
      valid.forEach((s, index) => {
        const jd = Cesium.JulianDate.fromDate(new Date(s.t));
        const pos = Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.alt_km * 1000);
        sampledPos.addSample(jd, pos);
        
        if (index === 0) startTime = jd.clone();
        if (index === valid.length - 1) endTime = jd.clone();
        
        // Track global time range
        if (!earliestTime || Cesium.JulianDate.lessThan(jd, earliestTime)) {
          earliestTime = jd.clone();
        }
        if (!latestTime || Cesium.JulianDate.greaterThan(jd, latestTime)) {
          latestTime = jd.clone();
        }
      });

      viewer.entities.add({
        id: sat.norad_id,
        name: sat.name,
        position: sampledPos,
        availability: new Cesium.TimeIntervalCollection([
          new Cesium.TimeInterval({
            start: startTime,
            stop: endTime,
          }),
        ]),
        point: {
          pixelSize: 7,
          color: Cesium.Color.YELLOW,
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 2,
        },
        path: {
          show: true,
          leadTime: 0,
          trailTime: 600,
          width: 2,
          material: Cesium.Color.YELLOW.withAlpha(0.5),
        },
      });
    });

    // 🔧 Set clock bounds to match data range
    if (earliestTime && latestTime) {
      viewer.clock.startTime = earliestTime.clone();
      viewer.clock.stopTime = latestTime.clone();
      
      // Start at current time if it's within range, otherwise start at beginning
      if (Cesium.JulianDate.greaterThanOrEquals(now, earliestTime) && 
          Cesium.JulianDate.lessThanOrEquals(now, latestTime)) {
        viewer.clock.currentTime = now.clone();
      } else {
        viewer.clock.currentTime = earliestTime.clone();
      }
    }

    // Zoom to satellites once
    if (!hasZoomed.current && viewer.entities.values.length > 0) {
      viewer.zoomTo(viewer.entities);
      hasZoomed.current = true;
    }
  }, [viewer, satelliteData]);

  // 👆 Click to show satellite info
  useEffect(() => {
    if (!viewer) return;
    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    let selectedLabel = null;

    handler.setInputAction((movement) => {
      if (selectedLabel) {
        viewer.entities.remove(selectedLabel);
        selectedLabel = null;
      }

      const picked = viewer.scene.pick(movement.position);
      if (Cesium.defined(picked) && picked.id?.position) {
        const entity = picked.id;
        const position = entity.position.getValue(viewer.clock.currentTime);
        
        if (position) {
          const carto = Cesium.Cartographic.fromCartesian(position);
          const alt = carto.height / 1000;

          selectedLabel = viewer.entities.add({
            position: position,
            label: {
              text: `${entity.name}\nAltitude: ${alt.toFixed(1)} km`,
              font: "14pt monospace",
              style: Cesium.LabelStyle.FILL_AND_OUTLINE,
              outlineWidth: 2,
              verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
              pixelOffset: new Cesium.Cartesian2(0, -9),
              fillColor: Cesium.Color.WHITE,
            },
          });
        }
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

    return () => handler.destroy();
  }, [viewer]);

  return (
    <div
      ref={cesiumContainer}
      style={{
        width: "100vw",
        height: "100vh",
        overflow: "hidden",
        margin: 0,
        padding: 0,
      }}
    />
  );
}

export default Globe;