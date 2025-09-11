import argparse

from robofast import observatory

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Robotic observations')
    parser.add_argument('--config', dest=config_file, default='observatory_minerva.yaml', help='Configuration file for the observatory')
    opt = parser.parse_args()

    root_dir = Path(__file__).resolve().parent
    config_file = root_dir / "config" / opt.config_file
    observatory.observe(config_file)
