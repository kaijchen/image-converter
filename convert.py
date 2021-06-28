#!/usr/bin/env python3

import os
import sys
import shutil
import subprocess
import logging
import json
import hashlib
import stat

jsonSep = (',', ':')

J_MODE = 'mode'
J_DIRENT = 'dirents'
J_SIZE = 'size'
J_HASH = 'hash'
J_LINK = 'link'

def mkdir(path, skipIfExist=False):
    if os.path.exists(path):
        if skipIfExist and os.path.isdir(path):
            return False
        shutil.rmtree(path)
    os.mkdir(path)
    return True

def relPath(*paths):
    def absPath(*subpaths):
        return os.path.join(*paths, *subpaths)
    return absPath

def sha256sum(path):
    p1 = subprocess.Popen(['sha256sum', path], stdout=subprocess.PIPE)
    p2 = subprocess.Popen(['awk', '{print $1}'], stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    checksum = p2.communicate()[0].decode('utf-8').removesuffix('\n')
    return checksum

class Layer:
    def __init__(self, path):
        self.src = path
        dirpath, _ = os.path.split(path)
        _, self.id = os.path.split(dirpath)

    def unpack(self, dst):
        mkdir(dst, skipIfExist=True)
        path = os.path.join(dst, self.id, 'layer')
        os.makedirs(path)
        subprocess.run(['tar', '-xf', self.src, '-C', path])
        return UnpackedLayer(path)

class UnpackedLayer:
    def __init__(self, path):
        self.src = path
        dirpath, _ = os.path.split(path)
        _, self.id = os.path.split(dirpath)

    def pack(self, dst):
        mkdir(dst, skipIfExist=True)
        dirpath = os.path.join(dst, self.id)
        os.makedirs(dirpath)
        path = os.path.join(dirpath, 'layer.tar')
        subprocess.run(['tar', '-cf', path, '-C', self.src, '.'])
        return Layer(path)
    
    def convert(self, metadata, pool, hashfunc=sha256sum):
        root = {J_MODE: os.lstat(self.src).st_mode, J_DIRENT: {}}
        note = {self.src: root}
        for parent, dirs, files in os.walk(self.src):
            dirents = note[parent][J_DIRENT]
            for d in dirs:
                path = os.path.join(parent, d)
                s = os.lstat(path)
                dirent = {J_MODE: s.st_mode, J_DIRENT: {}}
                dirents[d] = dirent
                note[path] = dirent
            for f in files:
                path = os.path.join(parent, f)
                s = os.lstat(path)
                if stat.S_ISLNK(s.st_mode):
                    dirent = {J_MODE: s.st_mode, J_SIZE: s.st_size, J_LINK: os.readlink(path)}
                else:
                    hash = hashfunc(path)
                    dirent = {J_MODE: s.st_mode, J_SIZE: s.st_size, J_HASH: hash}
                    target = os.path.join(pool, hash)
                    if not os.path.exists(target):
                        shutil.copyfile(path, target)
                dirents[f] = dirent 
        mkdir(self.src)
        with open(os.path.join(self.src, metadata), 'w') as fp:
            json.dump(root, fp, separators=jsonSep)

class Image:
    def __init__(self, path, pool='pool'):
        self._name = path.removesuffix('.tar')
        self._srcTar = path
        self._src = relPath(self._name, 'orig')
        self._dst = relPath(self._name, 'lazy')
        self._tmp = relPath(self._name, 'temp')
        self._pool = pool
        self._target = self._name + '-lazy.tar'
        mkdir(self._name, skipIfExist=True)
        mkdir(self._pool, skipIfExist=True)

    def convert(self):
        self._untar()
        self._loadManifest()
        self._unpackLayers()
        self._assembleLayers()
        self._writeConfigs()
        self._assembleTarget()

    def _assembleTarget(self):
        subprocess.run(['tar', '-cf', self._target, '-C', self._dst(), '.'])

    def _assembleLayers(self):
        mkdir(self._dst())
        self._config['rootfs']['diff_ids'] = []
        for layer in self._unpackedLayers:
            layer.convert('metadata.json', self._pool)
            packedLayer = layer.pack(self._dst())
            checksum = 'sha256:' + sha256sum(packedLayer.src)
            self._config['rootfs']['diff_ids'].append(checksum)
            logging.info(f'assembled layer {checksum}')
            shutil.copyfile(self._src(layer.id, 'VERSION'), self._dst(layer.id, 'VERSION'))
            shutil.copyfile(self._src(layer.id, 'json'), self._dst(layer.id, 'json'))

    def _writeConfigs(self):
        configHash = hashlib.sha256(json.dumps(self._config, separators=jsonSep).encode('ascii')).hexdigest()
        configName = configHash + '.json'
        with open(self._dst(configName), 'w') as fp:
            json.dump(self._config, fp, separators=jsonSep)
        self._manifest[0]['Config'] = configName
        tags = []
        for tag in self._manifest[0]['RepoTags']:
            name, ver = tag.split(':')
            newver = ver + '-lazy'
            self._repositories[name][newver] = self._repositories[name][ver]
            del self._repositories[name][ver]
            tags.append(':'.join([name, newver]))
        self._manifest[0]['RepoTags'] = tags
        with open(self._dst('repositories'), 'w') as fp:
            json.dump(self._repositories, fp, separators=jsonSep)
            fp.write('\n')
        with open(self._dst('manifest.json'), 'w') as fp:
            json.dump(self._manifest, fp, separators=jsonSep)
            fp.write('\n')

    def _unpackLayers(self):
        mkdir(self._tmp())
        self._unpackedLayers = []
        for layer in self._layers:
            unpackedLayer = layer.unpack(self._tmp())
            self._unpackedLayers.append(unpackedLayer)

    def _untar(self):
        filename, dirname = self._srcTar, self._src()
        logging.info(f'untaring {filename}')
        if not mkdir(dirname, skipIfExist=True):
            logging.info(f'directory "{dirname}" already exists, skipping untar')
            return
        code = subprocess.call(['tar', '-xf', filename, '-C', dirname])
        if code != 0:
            logging.fatal(f'failed to untar {filename}, exitcode {code}')

    def _loadManifest(self):
        with open(self._src('manifest.json')) as fp:
            self._manifest = json.load(fp)
        with open(self._src('repositories')) as fp:
            self._repositories = json.load(fp)
        with open(self._src(self._manifest[0]['Config'])) as fp:
            self._config = json.load(fp)
        self._layers = [Layer(self._src(x)) for x in self._manifest[0]['Layers']]
        repoTags = self._manifest[0]['RepoTags']
        logging.info(f'parse manifest success, RepoTags = {repoTags}')

if __name__ == '__main__':
    logging.getLogger().setLevel(logging.DEBUG)
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} tarball')
        sys.exit(-1)
    Image(sys.argv[1]).convert()
