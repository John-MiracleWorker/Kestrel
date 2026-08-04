import {
  DESTINATIONS,
  destinationLocation,
  formatAppLocation,
  type AppDestination,
  type AppLocation,
} from "./destinations";

export type NavigationRailProps = {
  location: AppLocation;
  onNavigate: (destination: AppDestination) => void;
  disabled?: boolean;
};

export function NavigationRail({
  location,
  onNavigate,
  disabled = false,
}: NavigationRailProps) {
  return (
    <div
      className="workbench-navigation-rail"
      inert={disabled ? true : undefined}
    >
      <div className="workbench-shell-brand">
        <span className="workbench-shell-brand-mark" aria-hidden="true">
          K
        </span>
        <span className="workbench-navigation-copy">
          <strong>Kestrel</strong>
          <small>Wildflower Workbench</small>
        </span>
      </div>
      <nav
        className="workbench-destinations"
        aria-label="Workbench destinations"
      >
        {DESTINATIONS.map((destination) => {
          const Icon = destination.icon;
          const current = destination.id === location.destination;
          return (
            <a
              data-destination={destination.id}
              href={formatAppLocation(destinationLocation(destination.id))}
              aria-label={destination.label}
              aria-disabled={disabled || undefined}
              aria-current={current ? "page" : undefined}
              tabIndex={disabled ? -1 : undefined}
              title={destination.label}
              key={destination.id}
              onClick={(event) => {
                if (disabled) {
                  event.preventDefault();
                  return;
                }
                if (
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                ) {
                  return;
                }
                event.preventDefault();
                onNavigate(destination.id);
              }}
            >
              <Icon size={19} aria-hidden="true" />
              <span className="workbench-destination-label">
                {destination.label}
              </span>
              {current ? <span className="sr-only">Current</span> : null}
            </a>
          );
        })}
      </nav>
      <div className="workbench-navigation-bloom" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <p className="workbench-navigation-boundary">
        <span aria-hidden="true">●</span>
        <span className="workbench-navigation-copy">
          Local · private · single owner
        </span>
      </p>
    </div>
  );
}
